#!/usr/bin/env python3
"""Upgrade 10: the morning report (web/morningreport.py; jobboard.py keeps small "# U10" hooks).

What Jason reads over breakfast: everything the board did since the report before it - jobs,
project queues and bench sets - with what needs him at the top.

Every number and every reason comes from a record the board itself wrote (a job's meta.json, a
queue file, a bench run), never from the model. The markdown is built by `report.build` from
`agentjobs_upgrades/` (upgrade 10's tested logic, not rewritten here); this module is the glue
that turns real job records into the shape it wants, saves the file and tells the phone.

  * Where it lives: state/reports/YYYY-MM-DD.md (env JOBBOARD_REPORTS) plus index.json, which
    says when the last report ended - the next one covers everything after that. Settings are
    state/report-settings.json (env JOBBOARD_REPORT_SETTINGS): what time the report is made
    (07:00 local) and whether the model gets a say. Same rule as every other record: outside
    anything the sandbox mounts, so no job can write its own report or move the window.
  * When it is made: once the clock passes the set time (a report missed while a long job ran is
    made as soon as the worker is free), when a project queue finishes or halts after a failed
    task, and when "Make report now" is pressed. An automatic report with nothing new to say is
    not written; a pressed button always writes one.
  * The model paragraph comes last, labelled "Qwen's summary", and only when the model server is
    loaded and nothing else is using it. Otherwise it is skipped - the report is never late
    because a GPU is busy.
  * A queue task that never ran has no job record, so its line links to the project page: that
    is what jobboard's /jobs/<name> alias answers a folder name with.

Nothing here may raise into the worker, so every entry point the board calls catches.
Standard library only.
"""
from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import decisions   # U08: what the last human decision left (merged / rejected / asked)
import notifycfg   # U09b: the report-ready push already exists (event "morning_report")
from agentjobs_upgrades import report as rp
from agentjobs_upgrades import vision

# ------------------------------------------------------------------ settings

DEFAULT_TIME = "07:00"
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d{1,3})?\.md$")      # 2026-11-11.md, -2.md, ...
INDEX_NAME = "index.json"
INDEX_MAX = 200                    # how many reports the page lists
FIRST_WINDOW_S = 24 * 3600         # what the very first report covers
CHAT_URL = "http://127.0.0.1:8080/v1/chat/completions"
SUMMARY_MAX = 700                  # the model's paragraph, in characters
SUMMARY_FACTS = 5000               # how much of the report the model is shown


@dataclass
class ReportSettings:
    time: str = DEFAULT_TIME       # what time the report is made, local
    ask_model: bool = True         # ask for "Qwen's summary" when the model is idle


def settings(path, write_defaults=False) -> ReportSettings:
    """ReportSettings from the settings file. Missing or broken = the defaults, and reading
    never creates the file unless write_defaults is set."""
    path = Path(path)
    raw = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    except (OSError, ValueError):
        raw = {}
    s = ReportSettings()
    if raw:
        hm = str(raw.get("time") or "").strip()
        if TIME_RE.fullmatch(hm):
            s.time = hm
        s.ask_model = bool(raw.get("ask_model", True))
    if write_defaults:
        save(path, s)
    return s


def save(path, s: ReportSettings):
    """Write the settings file (0600, like every other board setting)."""
    path = Path(path)
    data = {"time": s.time, "ask_model": bool(s.ask_model)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    return data


def save_form(path, form) -> list[str]:
    """The Reports page's settings form -> saved, or the reasons it was not saved."""
    cur = settings(path)
    get = lambda k: (form.get(k, [""])[0]).strip()
    hm = get("time") or cur.time
    errs = []
    if not TIME_RE.fullmatch(hm):
        errs.append("The time must look like 07:00.")
    if errs:
        return errs
    save(path, ReportSettings(time=hm, ask_model=bool(form.get("ask_model"))))
    return []


# ------------------------------------------------------------------ saved reports

def index_of(reports) -> list[dict]:
    """The saved reports, oldest first: [{name, day, made, since, until, jobs, waiting, trigger}]."""
    try:
        raw = json.loads((Path(reports) / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for e in raw:
        if isinstance(e, dict) and DAY_RE.fullmatch(str(e.get("name") or "")):
            out.append(e)
    return sorted(out, key=lambda e: float(e.get("until") or 0))


def save_index(reports, entries):
    path = Path(reports) / INDEX_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(list(entries)[-INDEX_MAX:], indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)
    return entries


def since_for(reports, now=None) -> float:
    """Where this report starts: when the last one ended. The very first report looks back a day."""
    now = time.time() if now is None else now
    entries = index_of(reports)
    if not entries:
        return now - FIRST_WINDOW_S
    try:
        last = float(entries[-1].get("until") or 0)
    except (TypeError, ValueError):
        last = 0.0
    return last if 0 < last <= now else now - FIRST_WINDOW_S


def slot(day_ts, hm) -> float:
    """Today's (or that day's) moment at HH:MM, local."""
    d = datetime.fromtimestamp(day_ts).replace(hour=int(hm[:2]), minute=int(hm[3:]),
                                               second=0, microsecond=0)
    return d.timestamp()


def due(settings_path, reports, now=None) -> bool:
    """Is the daily report overdue? Once the clock passes the set time and none has been made
    since that moment - so a report the board missed while a job ran comes out as soon as the
    worker is free, and it never happens twice in a day."""
    now = time.time() if now is None else now
    at = slot(now, settings(settings_path).time)
    if now < at:
        return False
    entries = index_of(reports)
    made = 0.0
    if entries:
        try:
            made = float(entries[-1].get("made") or 0)
        except (TypeError, ValueError):
            made = 0.0
    return made < at


def report_name(reports, entries, until) -> str:
    """reports/YYYY-MM-DD.md, with -2, -3 for a second report on the same day (a queue that
    finishes after the morning one)."""
    day = datetime.fromtimestamp(until).strftime("%Y-%m-%d")
    used = {str(e.get("name")) for e in entries}
    n = 1
    while True:
        name = f"{day}.md" if n == 1 else f"{day}-{n}.md"
        if name not in used and not (Path(reports) / name).exists():
            return name
        n += 1                               # without this the loop never ends on a second report


def read_report(reports, name) -> Optional[str]:
    """A saved report's text, or None. The name is a date file, never a path."""
    if not DAY_RE.fullmatch(str(name or "")):
        return None
    try:
        text = (Path(reports) / str(name)).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return text


# ------------------------------------------------------------------ job records -> facts

DONEISH = ("passed", "needs_review", "unverified")
ACTIVE = ("queued", "running", "stopping")


def one_line(text, limit=90) -> str:
    return " ".join(str(text or "").split())[:limit]


def final_summary(m):
    """The checks summary that stands: a re-run beats the one the gate left."""
    return (m.get("recheck") or {}).get("summary") or (m.get("verify") or {}).get("final") or {}


def report_status(m) -> str:
    """The board's status in report.py's words. A finished job is only as good as its checks, and
    a re-run (upgrade 1's Recheck button) is the standing result - the same precedence the
    dashboard and the check badges use - so the report never writes "failed" over a re-run that
    passed, while a gate verdict of needs_review or stuck still reads as what it was."""
    st = m.get("status")
    if st != "done":
        return st or "?"
    vst = (((m.get("recheck") or {}).get("summary") or {}).get("status")
           or (m.get("verify") or {}).get("status"))
    if vst in ("needs_review", "stuck", "failed", "passed"):
        return vst
    return "unverified"


def tests_passed(m) -> int:
    return sum(int(r.get("passed") or 0) for r in final_summary(m).get("results") or [])


def attempts_count(m) -> int:
    """Best-of-N tries this job used (upgrade 5); an ordinary job counts as one."""
    log = m.get("attempt_log") or []
    if log:
        return len(log)
    try:
        return max(1, int(m.get("attempts") or 1))
    except (TypeError, ValueError):
        return 1


def check_lines(m) -> list[str]:
    """One short line per check that failed, required ones first (an advisory failure is only
    a warning, so it never stands in for the reason a job failed)."""
    required, advisory = [], []
    for r in final_summary(m).get("results") or []:
        if r.get("ok"):
            continue
        if r.get("type") == "vision":
            issues = r.get("issues") or []
            line = issues[0] if issues else f"{r.get('name')} does not look right"
        elif r.get("timed_out"):
            line = f"{r.get('name')} took too long"
        else:
            block = str(r.get("first_error") or "").strip()
            line = next((ln.strip() for ln in block.splitlines() if ln.strip()), "")
            if not line:
                bad = int(r.get("failed") or 0)
                line = (f"{r.get('name')}: {bad} failing" if bad
                        else f"{r.get('name')} (exit {r.get('exit_code')})")
        (advisory if r.get("mode") == "advisory" else required).append(one_line(line))
    return required or advisory


def first_failing_line(m) -> str:
    lines = check_lines(m)
    return lines[0] if lines else ""


def watchdog_stop_reason(m) -> str:
    """The watchdog's own words, when it ended the job (not the nudges it sent first)."""
    for a in reversed(m.get("watchdog") or []):
        if a.get("kind") == "stop":
            return one_line(a.get("reason"), 160)
    return ""


def safety_command(m) -> str:
    """What the guard would not let the agent run - the command or path it caught."""
    for a in reversed(m.get("watchdog") or []):
        if a.get("kind") != "safety_stop":
            continue
        cmd = one_line(a.get("command") or a.get("path"), 160)
        if cmd:
            return cmd
        reason = str(a.get("reason") or "")
        if "Safety stop:" in reason:
            reason = reason.split("Safety stop:", 1)[-1]
        return one_line(reason, 160)
    note = str(m.get("fail_note") or "")
    if "Safety stop:" in note:
        return one_line(note.split("Safety stop:", 1)[-1], 160)
    return ""


def approval(m) -> str:
    """What upgrade 8 left decided. A finished job nobody has touched is still waiting: nothing
    on this board merges by itself."""
    if m.get("decision_log"):
        st = decisions.state(m)
        for key, word in (("merge", "merged"), ("reject", "rejected"), ("changes", "changes"),
                          ("conflict", "conflict")):
            if st.get(key):
                return word
    return "waiting"


def job_fact(m, blocked_ids=()) -> Optional[dict]:
    """One job record -> the dict report.build counts. None for a record with nothing to say yet."""
    if not m or m.get("kind") == "bench" or m.get("status") == "queued":
        return None
    status = report_status(m)
    started = m.get("started") or m.get("created")
    fact = {"id": str(m.get("id") or ""), "title": one_line(m.get("job") or m.get("id"), 80),
            "project": one_line(m.get("folder"), 40) or "Other jobs", "status": status,
            "started": started, "finished": m.get("finished"),
            "rounds": len(((m.get("verify") or {}).get("rounds")) or []),
            "rollbacks": int((m.get("checkpoints") or {}).get("rollbacks") or 0),
            "attempts": attempts_count(m), "tests_passed": tests_passed(m)}
    if status in DONEISH:
        fact["approval"] = approval(m)
    elif status == "safety_stop":
        fact["safety_command"] = safety_command(m)
    elif status in ("failed", "stopped"):
        wr = watchdog_stop_reason(m)
        if wr:
            fact["watchdog_reason"] = wr
        ff = first_failing_line(m)
        if ff:
            fact["first_failure"] = ff
        fact["banner"] = one_line(m.get("fail_note") or
                                  ((m.get("verify") or {}).get("banner") or {}).get("text") or
                                  ("Stopped by you" if status == "stopped" else ""), 160)
    if m.get("id") in blocked_ids:
        fact["queue_blocked"] = True
    return fact


def bench_fact(m, verdict="") -> dict:
    """A bench set is not a job: it gets its own Bench section with its overall verdict."""
    return {"label": one_line(m.get("job") or m.get("id"), 60) or m.get("id"),
            "finished": m.get("finished") or m.get("created"),
            "verdict": verdict or (("failed: " + one_line(m.get("fail_note"), 60))
                                   if m.get("status") != "done" else "finished"),
            "summary": one_line(m.get("fail_note"), 120)}


def queue_rows(queues) -> list[tuple]:
    """[(folder, queue state, file mtime)] for every queue file."""
    out = []
    try:
        paths = sorted(Path(queues).glob("*.json"))
    except (OSError, TypeError):
        return out
    for p in paths:
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
            mtime = p.stat().st_mtime
        except (OSError, ValueError):
            continue
        if isinstance(st, dict) and isinstance(st.get("tasks"), list):
            out.append((p.stem, st, mtime))
    return out


def blocked_job_ids(queues) -> set:
    """The jobs whose failure halted a queue - the report says so on their line."""
    out = set()
    for _folder, st, _mtime in queue_rows(queues):
        if st.get("status") != "stopped":
            continue
        for t in st.get("tasks") or []:
            if t.get("state") == "failed" and t.get("job_id"):
                out.add(str(t["job_id"]))
    return out


def queue_facts(queues, since, until, blocked=None) -> list[dict]:
    """Tasks a queue never got to: skipped when a failure let the queue carry on, or still
    waiting while it stands stopped. They have no job record, so their line names the project."""
    out = []
    for folder, st, mtime in queue_rows(queues):
        if not (since <= mtime < until):
            continue
        halted = st.get("status") == "stopped"
        for t in st.get("tasks") or []:
            state = t.get("state")
            if state == "skipped":
                word = "skipped"
            elif halted and state in ("queued", "interrupted"):
                word = "blocked"
            else:
                continue
            out.append({"id": folder, "project": folder, "title": one_line(t.get("title") or t.get("id"), 80),
                        "status": word, "started": mtime, "finished": mtime,
                        "rounds": 0, "rollbacks": 0, "attempts": 1, "tests_passed": 0})
    return out


def record_stamp(m):
    """When a record counts for: when it ended, else when it started. None when the record holds
    something that is not a time - a strange record is left out of the window, not fatal."""
    for key in ("finished", "started"):
        v = m.get(key)
        if isinstance(v, (int, float)) and v:
            return float(v)
    return None


def gather(jobs, queues=None, since=None, until=None, bench_verdict=None) -> tuple:
    """Everything in the window: job facts, bench sets and the queue tasks that never ran."""
    facts, bench = [], []
    blocked = blocked_job_ids(queues) if queues else set()
    ids = set()
    for m in jobs or []:
        if not isinstance(m, dict):
            continue                              # not a record at all: skip it, keep the report
        ids.add(str(m.get("id") or ""))
        stamp = record_stamp(m)
        if stamp is None or not (since <= stamp < until):
            continue
        if m.get("kind") == "bench":
            bench.append(bench_fact(m, bench_verdict(m) if bench_verdict else ""))
            continue
        fact = job_fact(m, blocked)
        if fact:
            facts.append(fact)
    if queues:
        facts += queue_facts(queues, since, until, blocked)
    return facts, bench


# ------------------------------------------------------------------ the model's paragraph

SUMMARY_PROMPT = ("The job board on this PC wrote the report below overnight, from its own job "
                  "records. Write at most two short sentences for Jason: what went well, and what "
                  "needs his attention. Use only what is in the report - never add a fact, a name "
                  "or a number that is not there.\n\n")


def qwen_summary(facts_md, *, status="loaded", busy=False, call=None, url=CHAT_URL,
                  timeout=120, max_tokens=220) -> Optional[str]:
    """The optional paragraph at the end of a report. None - and no call at all - unless the
    model server is loaded and nothing else is using it (the GPU answers one stream)."""
    if busy or status != "loaded":
        return None
    body = {"model": "local", "temperature": 0.3, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": SUMMARY_PROMPT + str(facts_md)[-SUMMARY_FACTS:]}]}
    try:
        text = (call or vision.http_call(url, timeout=timeout))(body)
    except Exception:
        return None
    if not isinstance(text, str):
        return None      # an answer that is not prose (a dict, a number) is no paragraph
    text = " ".join(text.replace("\u2018", "'").replace("\u2019", "'").split())
    if not text:
        return None
    return text[:SUMMARY_MAX]


# ------------------------------------------------------------------ make one report

def make(*, reports, settings_path, jobs, queues=None, notify_path=None, base_url="",
         force=False, trigger="manual", now=None, bench_verdict=None, model=None) -> Optional[dict]:
    """Build a report, save it as reports/YYYY-MM-DD.md, remember it in index.json and tell the
    phone. Returns the index entry, or None when there was nothing new to say and nobody pressed
    the button. Never raises: a broken record must not stop the worker."""
    now = time.time() if now is None else now
    since = since_for(reports, now)
    facts, bench = gather(jobs, queues, since, now, bench_verdict)
    if not (facts or bench) and not force:
        return None
    s = settings(settings_path)
    summary_fn = None
    if model and s.ask_model:
        def summary_fn(f):
            # The facts are already written: a model that hangs up must not cost Jason the
            # report. And an answer that is blank is no paragraph - no empty heading either.
            try:
                out = model(f)
            except Exception:
                return None
            return out if out and str(out).strip() else None
    try:
        md = rp.build(facts, bench, since=since, until=now, base_url=base_url,
                      qwen_summary=summary_fn)
    except Exception:
        return None
    entries = index_of(reports)
    name = report_name(reports, entries, now)
    waiting = sum(1 for f in facts if f.get("approval") == "waiting" or f.get("status") == "safety_stop")
    entry = {"name": name, "day": name[:10], "made": now, "since": since, "until": now,
             "jobs": len(facts), "bench": len(bench), "waiting": waiting, "trigger": trigger}
    try:
        Path(reports).mkdir(parents=True, exist_ok=True)
        (Path(reports) / name).write_text(md, encoding="utf-8")
    except OSError:
        return None
    save_index(reports, entries + [entry])
    if notify_path:
        try:
            notifycfg.report_ready(notify_path, lines=push_lines(facts, bench, since))
        except Exception:
            pass
    return entry


def push_lines(facts, bench, since) -> list[str]:
    """What the phone shows: counts only, like every other alert."""
    done = sum(1 for f in facts if f.get("status") == "passed")
    bad = sum(1 for f in facts if f.get("status") in rp.FAILED)
    waiting = sum(1 for f in facts if f.get("approval") == "waiting" or f.get("status") == "safety_stop")
    lines = [f"{done} done, {bad} failed since " + time.strftime("%H:%M", time.localtime(since))]
    second = f"{waiting} waiting for you" if waiting else (
        f"{len(bench)} bench sets finished" if bench else "Nothing needs you")
    return [lines[0], second]


def tick(*, reports, settings_path, jobs, queues=None, **kw) -> Optional[dict]:
    """The worker's clock check: make the daily report if it is due. Never raises."""
    try:
        if not due(settings_path, reports):
            return None
        return make(reports=reports, settings_path=settings_path, jobs=jobs, queues=queues,
                    trigger="clock", **kw)
    except Exception:
        return None


def queue_finished(*, reports, settings_path, queues, jobs, folder, **kw) -> Optional[dict]:
    """A project queue came to an end - every task done, or it halted after a failure. The
    overnight work is over, so the report comes out now rather than at 07:00. A queue that was
    cancelled is left alone: Jason stopped it himself and knows."""
    try:
        if not folder:
            return None
        state = None
        for name, st, _mtime in queue_rows(queues):
            if name == folder:
                state = st.get("status")
                break
        if state not in ("finished", "stopped"):
            return None
        return make(reports=reports, settings_path=settings_path, queues=queues, jobs=jobs,
                    trigger="queue", **kw)
    except Exception:
        return None


# ------------------------------------------------------------------ the pages

def md_inline(text) -> str:
    """Escape first, then the few markdown marks report.py writes: `code`, **bold**, _italic_
    and [text](url). A link only goes somewhere this board can answer."""
    s = html.escape(str(text), quote=True)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w])_([^_]+)_(?![\w])", r"<em>\1</em>", s)

    def link(mo):
        txt, url = mo.group(1), mo.group(2)
        ok = re.match(r"^https?://", url) or (url.startswith("/") and not url.startswith("//"))
        return f'<a href="{url}">{txt}</a>' if ok else txt

    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, s)
    # report.py writes the bundle as a plain path; on the page it is a button's worth of a link.
    return re.sub(r"([–-]) export for review: (/api/jobs/[0-9A-Za-z_.-]+/bundle)",
                  r'\1 <a href="\2">Export for review</a>', s)


def md_html(md) -> str:
    """A report's markdown as phone-friendly HTML. Headings, lists, links, nothing clever."""
    out, in_ul = [], False
    for raw in str(md).splitlines():
        line = raw.rstrip()
        if not line:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            continue
        head = re.match(r"^(#{1,4})\s+(.*)$", line)
        if head:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            lvl = min(4, len(head.group(1)))
            out.append(f"<h{lvl}>{md_inline(head.group(2))}</h{lvl}>")
            continue
        if line.startswith("- "):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{md_inline(line[2:])}</li>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        out.append(f"<p>{md_inline(line)}</p>")
    if in_ul:
        out.append("</ul>")
    return "".join(out)


def when(ts) -> str:
    return time.strftime("%d %b %H:%M", time.localtime(ts)) if ts else ""


def day_title(day) -> str:
    """2026-11-11 reads as 11 Nov 2026."""
    try:
        return time.strftime("%d %b %Y", time.strptime(str(day), "%Y-%m-%d"))
    except (TypeError, ValueError):
        return str(day or "")


# Only .md rules, and nothing a phone would have to fight: report lines wrap, nothing has a
# fixed width. (A page-appended <style> would otherwise outrank the phone CSS.)
CSS = """<style>
.md h1{font-size:21px;margin:2px 0 10px}.md h2{font-size:16px;margin:16px 0 6px}
.md h3{font-size:14px;margin:12px 0 4px}.md p{margin:4px 0;word-break:break-word}
.md ul{margin:4px 0 10px;padding-left:20px}.md li{margin:5px 0;word-break:break-word}
.md code{background:var(--code);padding:1px 4px;border-radius:4px;font:13px ui-monospace,monospace}
.reps{list-style:none;margin:0;padding:0}.reps li{margin:9px 0}
</style>"""


def page_body(reports, settings_path, errors=(), note="", form=None, readonly=False,
              token_field="", token="") -> str:
    """The Reports tab: what is saved, "Make report now", and when the next one comes. One line
    per report - a five-column table reads badly on a phone."""
    s = settings(settings_path)
    entries = index_of(reports)
    errs = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
    err_html = f'<div class="card bad"><b>Not saved:</b><ul>{errs}</ul></div>' if errs else ""
    note_html = f'<p class="ok">{html.escape(note)}</p>' if note else ""
    tok = (f'<input type="hidden" name="{html.escape(token_field)}" '
           f'value="{html.escape(token, quote=True)}">' if token_field and token else "")
    val = ((form.get("time", [""])[0]).strip() if form else "") or s.time
    ask = bool(form.get("ask_model")) if form else s.ask_model
    rows = ""
    for e in reversed(entries):
        jobs = int(e.get("jobs") or 0)
        bench = int(e.get("bench") or 0)
        waiting = int(e.get("waiting") or 0)
        bits = [f'{jobs} job' + ("s" if jobs != 1 else "")]
        if bench:
            bits.append(f'{bench} bench set' + ("s" if bench != 1 else ""))
        bits.append(f'{waiting} waiting for you' if waiting else "nothing waiting")
        rows += (f'<li><a href="/reports/{html.escape(e["name"])}">'
                 f'{html.escape(day_title(e.get("day")))}</a> <span class="hint">'
                 f'{when(e.get("since"))} &rarr; {when(e.get("until"))} &middot; '
                 f'{html.escape(", ".join(bits))}</span></li>')
    listing = (f'<ul class="reps">{rows}</ul>' if rows
               else '<p class="mut">No reports yet. The next one comes at '
                    f'{html.escape(s.time)}, or press Make report now.</p>')
    ro = ('<p class="hint">This copy runs read-only, so nothing here can be changed.</p>') if readonly else ""
    btn = "" if readonly else (f'<form method="post" action="/report/now" style="display:inline">{tok}'
                               f'<button>Make report now</button></form>')
    form_html = "" if readonly else f"""<form method="post" action="/report/settings">
{tok}
<div class="row"><div><label>Make the report at</label>
<input type="text" name="time" value="{html.escape(val)}" style="width:120px">
<span class="hint">Local time, like 07:00. A report the board misses while a job runs comes out as
soon as the worker is free.</span></div>
<div><label style="font-weight:400;margin:0"><input type="checkbox" name="ask_model"
{"checked" if ask else ""}> End with "Qwen's summary" when the model is free</label>
<span class="hint">The model only adds that paragraph - every fact above it comes from the job
records. Skipped whenever the model is busy.</span></div></div>
<div class="brow" style="margin-top:12px"><button>Save</button></div></form>"""
    return f"""{CSS}<div class="card"><h2>Morning report</h2>
<p><b>The next one comes at {html.escape(s.time)}.</b> <span class="hint">Also made when a
project queue finishes, and by the button below.</span></p>
{note_html}{err_html}
{btn}</div>
<div class="card"><h2>Reports</h2>{listing}</div>
<div class="card"><h2>When to make one</h2>{ro}{form_html}</div>"""


def report_body(reports, name) -> Optional[str]:
    """One saved report, as HTML, with a way back and the raw text."""
    md = read_report(reports, name)
    if md is None:
        return None
    return (f'{CSS}<div class="card"><p class="bact"><a href="/reports">&larr; All reports</a> '
            f'&middot; <a href="/reports/{html.escape(str(name))}?raw=1" class="mut">Raw text</a></p>'
            f'<div class="md">{md_html(md)}</div></div>')
