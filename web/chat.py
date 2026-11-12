"""Job D: the Chat page - talk to the agent in the browser, from the PC or a phone, by voice.

jobboard.py keeps only small "# CHAT:" hooks: the routes, the CHATS folder and chat_ctx()
(what this module needs from the board). Like the other upgrade modules this one never
imports jobboard.

A chat turn is NOT a job:
  * it runs in its own thread, so an answer arrives while a job is running. The model may serve
    one request at a time, which is why the page says "waiting for the model" until the first
    line of output appears;
  * it goes through the SAME agent path a job uses - jobboard.check_record, then
    jobboard.launch_agent (which calls jobboard.agent_argv) with the offline local mode, the
    jail/agent script and the runaway watchdog, and Stop uses the board's own stop path. One
    message = one agent process, with --resume <session> for every message after the first, so
    it is one continuing conversation;
  * it has no verify gate and no checkpoints (upgrades.run_gate and CheckpointManager are never
    reached), and it is never a job record, so it never enters the job queue.

State, outside everything the sandbox mounts (like the job/plan/queue/report records):
    state/chats/<id>.json             the conversation: folder, mode, session id, messages (with time)
    state/chats/<id>/<turn id>.jsonl  that turn's raw agent stream - what the live log reads

Standard library only.
"""
from __future__ import annotations

import html, json, re, secrets, threading, time
from pathlib import Path

import dashboard      # U07: the SSE loop and the live-log parser a chat page reuses
import jailcfg        # JAIL: a local mode never gets the internet, chat turns included
import phonelayout    # U09a: the phone rules (700 px, thumb-sized taps) this page follows
import upgrades       # U01: EventLog + the watchdog settings a turn runs with
import voice_core     # VOICE: talk-back text is cleaned of markdown the same way as a report
from agentjobs_upgrades import watchdog as wdm

# The same id shape new_job() writes, so a turn id is also a container name (agentjob-<id>).
# tests/test_chat.py pins this against jobboard.JOB_ID_RE so the two cannot drift apart.
CHAT_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SESSION_RE = re.compile(r"[A-Za-z0-9-]{8,80}")

CHAT_MODE = "local-qwen"          # the chat agent named in the spec; MODES-checked per turn
ACTIVE = ("waiting", "running", "stopping")
LABELS = {"waiting": "Waiting for the model", "running": "The agent is answering",
          "stopping": "Stopping", "done": "Answered", "stopped": "Stopped by you",
          "safety_stop": "Safety stop", "failed": "Failed", "interrupted": "Interrupted",
          "refused": "Refused"}
MSG_MAX = 20000                   # one chat message (the agent's prompt)
ANSWER_MAX = 20000                # the agent's reply as saved in the conversation
SPEECH_MAX = 600                  # VOICE: longer than this, say the start and point at the screen
SPEECH_TAIL = "The rest is on screen."
CHAT_COLS = ("Started", "Project", "About", "Messages", "State")   # the chats table, in order

LOCK = threading.RLock()          # one chat record is read-modified-written as a whole
TURNS: dict[str, threading.Event] = {}    # turn id -> set when Stop was pressed for that turn
_TURNS_LOCK = threading.Lock()


# ---------------------------------------------------------------- the record

def new_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)

def chat_file(chats, cid):
    """state/chats/<id>.json, or None for anything that is not an id: a route must never turn
    its URL into a path."""
    if not CHAT_ID_RE.fullmatch(cid or ""):
        return None
    return Path(chats) / (cid + ".json")

def chat_dir(chats, cid):
    if not CHAT_ID_RE.fullmatch(cid or ""):
        return None
    return Path(chats) / cid

def turn_log(chats, cid, turn_id):
    if not CHAT_ID_RE.fullmatch(cid or "") or not CHAT_ID_RE.fullmatch(turn_id or ""):
        return None
    return Path(chats) / cid / (turn_id + ".jsonl")

def ok_record(cid, rec):
    """A chat file is a plain file, so it is re-checked every time it is used (the same rule the
    worker applies to a queued job with check_record): folder and session id both end up in an
    argv or a path, and the id in the file must match the file it is in."""
    if not CHAT_ID_RE.fullmatch(cid or "") or not isinstance(rec, dict) or rec.get("id") != cid:
        return False
    folder = rec.get("folder")
    if not isinstance(folder, str) or not FOLDER_RE.fullmatch(folder) or folder == "eval":
        return False
    if not isinstance(rec.get("mode"), str) or not re.fullmatch(r"[a-z-]{1,30}", rec["mode"] or ""):
        return False
    sid = rec.get("session")
    if sid is not None and (not isinstance(sid, str) or not SESSION_RE.fullmatch(sid)):
        return False
    if not isinstance(rec.get("messages"), list):
        return False
    turn = rec.get("turn")
    if turn is not None:
        if not isinstance(turn, dict) or not CHAT_ID_RE.fullmatch(turn.get("id") or ""):
            return False
        if not isinstance(turn.get("text"), str) or turn.get("state") not in LABELS:
            return False
    return True

def load(chats, cid):
    """The conversation, or None when it is missing or is not the shape this module writes."""
    p = chat_file(chats, cid)
    if p is None or not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if ok_record(cid, rec) else None

def save(chats, rec):
    p = chat_file(chats, rec.get("id"))
    if p is None:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    tmp.replace(p)
    return True

def new_chat(chats, folder, mode=CHAT_MODE):
    rec = {"id": new_id(), "folder": folder, "mode": mode, "offline": True,
           "created": time.time(), "updated": time.time(), "title": "", "session": None,
           "turn": None, "messages": []}
    with LOCK:
        save(chats, rec)
    return rec

def list_chats(chats):
    """Every conversation, newest first - the list on the Chat page."""
    try:
        files = sorted(Path(chats).glob("*.json"), key=lambda p: p.name, reverse=True)
    except OSError:
        return []
    out = []
    for p in files:
        rec = load(chats, p.name[:-5])
        if rec:
            out.append(rec)
    return out

def current_turn(chats, cid):
    rec = load(chats, cid)
    return rec, ((rec or {}).get("turn") or {})

def patch_turn(chats, cid, turn_id, **kw):
    """Change the CURRENT turn's fields; a Stop pressed for a turn that already ended never
    lands on the next one."""
    with LOCK:
        rec = load(chats, cid)
        turn = (rec or {}).get("turn") or {}
        if turn.get("id") != turn_id:
            return None
        turn.update(kw)
        rec["turn"] = turn
        rec["updated"] = time.time()
        save(chats, rec)
        return rec

def mark_interrupted(chats):
    """A chat turn that was running when the board stopped cannot be resumed by itself: say so
    on the page instead of leaving it looking live for ever (like mark_interrupted for jobs)."""
    n = 0
    for rec in list_chats(chats):
        turn = rec.get("turn") or {}
        if turn.get("state") in ACTIVE:
            patch_turn(chats, rec["id"], turn["id"], state="interrupted",
                       note="The board stopped while this turn was running")
            n += 1
    return n


# ---------------------------------------------------------------- one turn

def turn_meta(rec, turn):
    """The agent record for ONE turn, in exactly the shape agent_argv and check_record use, so a
    chat message runs the same command a job runs:
        agent local-qwen --offline --stream --name agentjob-<turn id> --in <folder> \
            [--resume <session>] <message>"""
    mode = rec["mode"]
    return {"id": turn["id"], "kind": "chat", "mode": mode, "folder": rec["folder"],
            "job": turn["text"], "offline": True if mode in jailcfg.LOCAL_MODES else bool(rec.get("offline", True)),
            "status": turn["state"], "created": turn.get("started"), "started": turn.get("started"),
            "finished": turn.get("finished"), "exit": None,
            "resume": rec.get("session"), "parent": None, "session": None}


class TurnCtx:
    """What jobboard.launch_agent needs to run one agent process: the same event log, the same
    watchdog, no checkpoints and no gate. Everything is keyed by the turn id, so the board's own
    Stop path (running/monitors, agentjob-<id>) finds it."""

    def __init__(self, meta, log_path, cfg, work):
        self.m, self.name, self.cfg = meta, meta["id"], cfg
        self.root = work / meta["folder"]
        self.events_path = Path(log_path)
        self.ev = upgrades.EventLog(self.events_path)
        proj = str(self.root) if upgrades.HOST_MODE else f"/work/{meta['folder']}"
        self.wd = wdm.Watchdog(upgrades.watchdog_settings(cfg), proj)
        self.ping = None if (upgrades.HOST_MODE or meta["mode"] == upgrades.FAKE_MODE) else wdm.model_server_ok
        self.actions = []
        self.last_error = ""
        self.mgr = None            # chat turns have no checkpoints
        self.runs = 0
        self.steps_seen = 0

    def on_plan(self, text, first):
        """A chat turn has no timeline and no PLAN.md checkpoints, so there is nothing to do."""


def turn_items(ctx, chats, cid, since=0):
    """The live log of the turn the page is watching (U07's parser, this turn's own file)."""
    rec, turn = current_turn(chats, cid)
    p = turn_log(chats, cid, turn.get("id"))
    if p is None:
        return [], since
    return ctx["events_at"](p, since)


def answer_text(items):
    """The agent's own words: its assistant text, never a tool call, a tool's output or the
    summary line the runner prints at the end. A turn that never said anything has no answer -
    that is how a turn stopped before the first words saves nothing."""
    said = [str(it["t"]).strip() for it in items
            if it.get("k") == "text" and str(it.get("t") or "").strip()]
    return "\n\n".join(said).strip()


def speech_text(text, max_chars=SPEECH_MAX):
    """VOICE: what the page reads aloud when a turn ends. Code and markdown go (clean_markdown
    drops code blocks and inline code), and a long answer is read as its start plus a nudge to
    look at the screen."""
    out = voice_core.clean_markdown(text or "")
    if len(out) > max_chars:
        cut = out[:max_chars]
        end = cut.rfind(" ")                       # stop on a whole word, never mid-word
        cut = cut[:end] if end > 0 else cut
        out = cut.rstrip(" ,;:.") + ". " + SPEECH_TAIL
    return out.strip()


def run_turn(ctx, cid, turn_id):
    """ONE chat turn, in the thread start_turn() started. Same safety re-check and agent path as
    a job (the record is re-read here, right before anything runs), and the turn's end state
    comes from the same mapping run_agent_job uses - minus the gate."""
    chats = ctx["chats"]
    rec, turn = current_turn(chats, cid)
    if rec is None or turn.get("id") != turn_id:
        return None
    meta = turn_meta(rec, turn)
    log = turn_log(chats, cid, turn_id)
    problem = ctx["check"](turn_id, meta)
    if problem:
        return patch_turn(chats, cid, turn_id, state="refused",
                          note=f"Refused: the chat turn failed the safety check ({problem})")
    if log is None:
        return patch_turn(chats, cid, turn_id, state="refused", note="Refused: bad turn log path")
    if ctx["is_local"](meta):     # JAIL: the same sandbox self-test a local job has to pass
        ok, out = ctx["selftest"]()
        if not ok:
            return patch_turn(chats, cid, turn_id, state="refused", note=jailcfg.selftest_failure_note(out))
    log.parent.mkdir(parents=True, exist_ok=True)
    cfg = upgrades.load_settings(ctx["settings"], write_defaults=False)
    tctx = TurnCtx(meta, log, cfg, ctx["work"])
    patch_turn(chats, cid, turn_id, state="running")
    if stop_pressed(turn_id):       # Stop landed before the agent even started: nothing to run
        return patch_turn(chats, cid, turn_id, state="stopped", note="Stopped by you",
                          finished=time.time(), how="user_stop", seconds=0)
    try:
        rc, fok, act = ctx["launch"](tctx, meta["job"], meta["resume"], 0, fresh_log=True)
    except Exception as ex:            # a missing agent script must not take the board down
        return patch_turn(chats, cid, turn_id, state="failed", note=f"Could not start the agent: {str(ex)[:200]}")
    how = (act.kind if act is not None else None)
    stopped = stop_pressed(turn_id) or (current_turn(chats, cid)[1] or {}).get("state") == "stopping"
    if stopped:
        how, state, note = "user_stop", "stopped", "Stopped by you"
    elif how == "safety_stop":
        state, note = "safety_stop", act.reason
    elif how == "stop":
        state, note = "failed", act.reason
    elif fok is not None:
        state, note = ("done", "") if fok else ("failed", "The agent finished with an error")
    else:
        state, note = ("done", "") if rc == 0 else ("failed", f"The agent exited {rc}")
    items, _ = ctx["events_at"](log, 0)
    answer = answer_text(items)[:ANSWER_MAX]
    sid = ctx["session_at"](log)
    with LOCK:
        rec = load(chats, cid)
        turn = (rec or {}).get("turn") or {}
        if turn.get("id") == turn_id:
            if sid:
                rec["session"] = sid
            if answer:
                rec["messages"].append({"role": "agent", "text": answer, "time": time.time()})
            turn.update(state=state, note=note, finished=time.time(), how=how, exit=rc,
                        seconds=int(time.time() - (turn.get("started") or time.time())))
            rec["turn"] = turn
            rec["updated"] = time.time()
            save(chats, rec)
    return rec


def turn_active(rec):
    return ((rec or {}).get("turn") or {}).get("state") in ACTIVE


def stop_pressed(turn_id):
    with _TURNS_LOCK:
        ev = TURNS.get(turn_id)
        return bool(ev and ev.is_set())


def start_turn(ctx, cid, text):
    """Add the message and run the turn in its own thread, so the POST can answer at once and a
    chat turn runs even while a job is running. Returns (turn id, note): one turn at a time per
    chat, because it is one continuing conversation."""
    chats = ctx["chats"]
    text = (text or "").strip()
    with LOCK:
        rec = load(chats, cid)
        if rec is None:
            return None, "That chat is gone."
        if turn_active(rec):
            return None, "The agent is still answering this chat - press Stop to end that turn first."
        if not text:
            return None, "Type or say something first."
        turn = {"id": new_id(), "text": text[:MSG_MAX], "state": "waiting", "started": time.time(),
                "finished": None, "how": None, "note": ""}
        rec["messages"].append({"role": "you", "text": turn["text"], "time": time.time()})
        if not rec.get("title"):
            rec["title"] = " ".join(text.split())[:80]
        rec["turn"] = turn
        rec["updated"] = time.time()
        save(chats, rec)
    tid = turn["id"]
    with _TURNS_LOCK:
        TURNS[tid] = threading.Event()
    threading.Thread(target=_run_turn_forget, args=(ctx, cid, tid), daemon=True).start()
    return tid, ""


def _run_turn_forget(ctx, cid, tid):
    try:
        run_turn(ctx, cid, tid)
    finally:
        with _TURNS_LOCK:
            TURNS.pop(tid, None)


def _kill_turn(ctx, cid, turn_id):
    """The board's own Stop path for one agent process: podman stop agentjob-<id> (or the process
    group in host test mode) and anything the agent's containers left behind.

    The loop is not decoration: Stop pressed a moment after Send lands before the agent process
    exists, and stop_process looks it up again each time (jobboard.running). It stops asking when
    the turn record is no longer this turn or is no longer active."""
    chats = ctx["chats"]
    for _ in range(20):
        try:
            ctx["stop_process"](turn_id)
            ctx["stop_containers"](turn_id)
        except Exception:      # best effort: a stop helper with no podman must not lose the turn
            pass
        _rec, turn = current_turn(chats, cid)
        if turn.get("id") != turn_id or turn.get("state") not in ACTIVE:
            return
        time.sleep(0.5)


def stop_turn(ctx, cid):
    """End the current turn. The record changes at once (so the page and a second press see
    it); the actual stop runs on its own thread, like the job page's Stop button.

    The record is read, marked and saved under one lock, and a turn that is no longer active is
    left alone: Stop pressed in the same moment the answer finished must not put a finished turn
    back to "stopping" (the page would wait for nothing for ever)."""
    chats = ctx["chats"]
    with LOCK:
        rec, turn = current_turn(chats, cid)
        if turn.get("state") not in ACTIVE or not turn.get("id"):
            return None
        tid = turn["id"]
        with _TURNS_LOCK:
            ev = TURNS.get(tid)
            if ev is not None:
                ev.set()      # the turn thread sees this, even before its agent has started
        turn["state"] = "stopping"
        rec["turn"] = turn
        rec["updated"] = time.time()
        save(chats, rec)
    threading.Thread(target=_kill_turn, args=(ctx, cid, tid), daemon=True).start()
    return tid


def turn_status(chats, rec):
    """What the page shows while a turn runs. A turn that has produced nothing yet is
    'waiting for the model': Basalt may be busy with a job, and a silent box looks broken."""
    turn = (rec or {}).get("turn") or {}
    state = turn.get("state") or "done"
    log = turn_log(chats, (rec or {}).get("id"), turn.get("id"))
    size = 0
    try:
        size = log.stat().st_size if log else 0
    except OSError:
        size = 0
    if state == "running" and not size:
        state = "waiting"
    return {"state": state, "label": LABELS.get(state, state), "note": str(turn.get("note") or ""),
            "turn": turn.get("id"), "folder": (rec or {}).get("folder"),
            "session": (rec or {}).get("session"),
            "seconds": turn.get("seconds"),
            "elapsed": int(time.time() - turn["started"]) if turn.get("started") and state in ACTIVE else None,
            "can_stop": state in ACTIVE, "log_size": size}


# ---------------------------------------------------------------- the live stream (U07)

def sse_meta(ctx, chats, cid):
    """dashboard.job_stream() ends the stream when meta["status"] stops being one of its ACTIVE
    states; a chat turn's states are translated here. None (the chat is gone) ends it too."""
    rec = load(chats, cid)
    if rec is None:
        return None
    s = turn_status(chats, rec)
    s["status"] = "running" if s["state"] in ACTIVE else "finished"
    return s


def stream(ctx, cid, log_off=0, *, poll_s=1.0, keepalive_s=15.0, linger_s=3.0):
    """SSE chunks for one chat: the turn's live log plus its status. There is no timeline card on
    a chat page, so no timeline ids (job_stream sends none when the folder has no timeline
    file) and the log offset is the only resume point."""
    chats = ctx["chats"]
    return dashboard.job_stream(cid, 0, log_off,
                                get_meta=lambda _cid: sse_meta(ctx, chats, cid),
                                get_status=lambda m: m,
                                get_log=lambda _cid, off: turn_items(ctx, chats, cid, off),
                                job_dir=chat_dir(chats, cid) or Path(chats),
                                poll_s=poll_s, keepalive_s=keepalive_s, linger_s=linger_s)


# ---------------------------------------------------------------- the page

def esc(x):
    return html.escape(str(x if x is not None else ""), quote=True)

def fmt_time(t):
    return time.strftime("%d %b %H:%M", time.localtime(t)) if t else ""


def bubble_html(m):
    """One saved message: yours or the agent's. Escaped; the text keeps its own line breaks."""
    you = m.get("role") == "you"
    return (f'<div class="bub {"you" if you else "agent"}"><div class="bhead">'
            f'<b>{"You" if you else "Agent"}</b><span class="bwhen">{esc(fmt_time(m.get("time")))}</span>'
            f'</div><div class="btext">{esc(m.get("text"))}</div></div>')


def turn_pill(s):
    return f'<span class="pill cs-{esc(s["state"])}">{esc(s["label"])}</span>'


def chats_body(chats, here=None, limit=50):
    rows = ""
    for rec in list_chats(chats)[:limit]:
        s = turn_status(chats, rec)
        title = rec.get("title") or "(no words yet)"
        state = turn_pill(s) if s["state"] in ACTIVE else ""
        rows += (f'<tr><td class="now"><a href="/chat/{esc(rec["id"])}">{esc(fmt_time(rec.get("created")))}</a></td>'
                 f'<td>{esc(rec.get("folder"))}</td>'
                 f'<td class="jobtxt" title="{esc(title)}">{esc(title[:90])}</td>'
                 f'<td class="now">{len(rec.get("messages") or [])}</td><td class="now">{state}</td></tr>')
    if not rows:
        rows = '<tr><td colspan=5 class=mut>No chats yet.</td></tr>'
    head = "".join(f"<th>{esc(c)}</th>" for c in CHAT_COLS)
    return (f'<div class="card"><h2>Chats</h2><div class="tbl"><table class="chats"><thead><tr>{head}</tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
            f'<p class="hint">A chat is one continuing conversation with the agent in one project folder: '
            f'each message runs it once, in the same session, with the sandbox and the watchdog a job '
            f'gets - but no checks and no checkpoints. Chats are saved in ~/agent-sandbox/state/chats.</p></div>')


def open_chat_body(ctx, rec, note=""):
    chats = ctx["chats"]
    cid = rec["id"]
    s = turn_status(chats, rec)
    bubbles = "".join(bubble_html(m) for m in rec.get("messages") or [])
    hidden = "" if s["state"] == "waiting" else " hidden"
    waiting = (f'<div class="sys" id="waiting"{hidden}>Waiting for the model - '
               f'a job may be using it.</div>')
    stopform = (f'<form method="post" action="/chat/{esc(cid)}/stop" id="chatstop">'
                f'<button class="ghost">Stop</button></form>') if s["can_stop"] else ""
    note_html = f'<p class="warn2">{esc(note)}</p>' if note else ""
    state_note = f" - {esc(s['note'])}" if s["note"] else ""
    return f"""
<div class="card chat"><h2>Chat <span class="mut">~/agent-work/{esc(rec["folder"])}</span>
 <a class="ghost2 btn-sm" href="/chat">New chat</a></h2>
<div class="chatlog" id="chatlog">{bubbles or '<p class="mut">Say something to start the conversation.</p>'}
<div id="live">{waiting}</div></div>
<form method="post" action="/chat/{esc(cid)}/send" id="chatform">
<label class="vh" for="chattext">Message</label>
<textarea name="text" id="chattext" data-voice="chat" maxlength="{MSG_MAX}" required
 placeholder="Ask the agent about this project. Hold the mic button to talk, or say &quot;agent&quot; and then your question."></textarea>
<button id="chatsend">Send</button></form>
<div class="chatfoot"><span class="hint c-{esc(s["state"])}" id="chatstate">{esc(s["label"])}{state_note}</span>
{stopform}{note_html}
</div>
<p class="hint">Each message runs the agent once in this folder ({esc(rec["mode"])}, offline, in the
sandbox, watched by the watchdog) and continues the same conversation. No checks are run and
nothing is committed for a chat turn.</p>
</div>
<script>
{CHAT_SCRIPT.replace("@@CID@@", json.dumps(cid)).replace("@@STATE@@", json.dumps(s["state"]))}
</script>"""


def new_chat_body(ctx, form=None, error=""):
    fopts = "".join(f'<option value="{esc(f)}"{" selected" if form and (form.get("folder") or [""])[0] == f else ""}>{esc(f)}</option>'
                    for f in ctx["folders"]())
    said = esc((form.get("text") or [""])[0]) if form else ""
    return f"""
<div class="card"><h2>New chat</h2><form method="post" action="/chat/new">
<label>Project folder (in ~/agent-work)</label>
<select name="folder"><option value="">Pick a folder...</option>{fopts}</select>
<label>Say</label>
<textarea name="text" data-voice="chat" maxlength="{MSG_MAX}" required
 placeholder="Ask the agent something about that project. You can hold the mic button and talk instead."></textarea>
{f'<p class="bad">{esc(error)}</p>' if error else ""}
<button>Start chat</button></form>
<p class="hint">The first message starts a new conversation; the ones after it continue it.</p></div>"""


CHAT_SCRIPT = """
(function(){
// IIFE: every page script shares one global scope, so a top-level name here could collide
// with another <script> on the page (that is how the job page's script lost its name once).
const cid=@@CID@@, start0=@@STATE@@, ACTIVE=["waiting","running","stopping"];
const log=document.getElementById("chatlog"), feed=document.getElementById("live"),
      stEl=document.getElementById("chatstate"), waitEl=document.getElementById("waiting");
if(!log||!feed) return;
let off=0, prev=start0, es=null, bubble=null, fails=0, said=false;
const VERB={cmd:"ran: ",edit:"edited: ",read:"read: ",other:"used: "};
function el(t,c,h){const d=document.createElement(t);if(c)d.className=c;if(h!==undefined)d.textContent=h;return d}
function atBottom(){return log.scrollTop+log.clientHeight>=log.scrollHeight-40}
let stick=atBottom();
log.addEventListener("scroll",function(){stick=atBottom()});
function scroll(){if(stick)log.scrollTop=log.scrollHeight}
function textBubble(){if(!bubble){bubble=el("div","bub agent");bubble.appendChild(el("div","btext"));feed.appendChild(bubble)}return bubble}
function add(it){
 if(it.k==="text"){const b=textBubble().lastChild;b.textContent+=(b.textContent?"\\n\\n":"")+it.t;scroll()}
 else if(it.k==="tool"){bubble=null;const d=el("div","tline "+(it.cat||"other"));d.appendChild(el("span","v",VERB[it.cat]||VERB.other));d.appendChild(el("code",null,it.t));feed.appendChild(d);scroll()}
 else if(it.k==="gate"){bubble=null;feed.appendChild(el("div","sys",it.t));scroll()}
 // a tool's output, the agent's thinking and raw log lines stay out of the chat: one tool call
 // is one line, and the answer is the agent's own words
}
function addItems(items){items.forEach(add)}
function apply(s){
 if(!s||!s.state) return;
 stEl.textContent=s.label+((s.note||s.elapsed)?(" - "+(s.note||(s.elapsed+"s"))):"");
 stEl.className="hint c-"+s.state;
 const sf=document.getElementById("chatstop"); if(sf) sf.hidden=!(s.can_stop);
 if(waitEl) waitEl.hidden=(s.state!=="waiting");
 const was=ACTIVE.indexOf(prev)>=0, now=ACTIVE.indexOf(s.state)>=0;
 prev=s.state;
 if(was&&!now&&!said){said=true;turnEnded(s)}
}
function turnEnded(s){
 if(es){es.close();es=null}
 if(s.note){stEl.textContent=s.label+" - "+s.note}
 talk();
}
function talk(){   // VOICE: read the agent's own words; the server has already dropped code
 try{fetch("/api/chat/"+cid+"/speech").then(function(r){return r.ok?r.json():{};}).then(function(j){
   if(j&&j.text&&window.AgentVoice)window.AgentVoice.say(j.text);}).catch(function(){});}catch(e){}
}
function start(){
 if(!window.EventSource){return}
 es=new EventSource("/api/chat/"+cid+"/events?log="+off);
 es.addEventListener("open",function(){fails=0});
 es.addEventListener("status",function(e){try{apply(JSON.parse(e.data))}catch(x){}});
 es.addEventListener("log",function(e){try{const d=JSON.parse(e.data);if(d.from<off)return;addItems(d.items);off=d.next}catch(x){}});
 es.addEventListener("end",function(){if(es)es.close();es=null});
 es.onerror=function(){if(es)es.close();es=null;fails++;
   if(fails<3&&ACTIVE.indexOf(prev)>=0)setTimeout(start,2000);
   else if(fails>=3&&ACTIVE.indexOf(prev)>=0){const b=el("button","ghost2","Reload to catch up");b.onclick=function(){location.reload()};feed.appendChild(b)}};
}
apply({state:start0});
if(ACTIVE.indexOf(start0)>=0)start();
})();
"""


CSS = """<style>
.chatlog{max-height:62vh;overflow-y:auto;margin:8px 0;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.bub{border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin:6px 0;background:var(--card)}
.bub.you{border-left:4px solid var(--acc)}
.bub.agent{border-left:4px solid var(--ok)}
.bhead{display:flex;gap:8px;align-items:baseline;font-size:12px;color:var(--mut)}
.btext{white-space:pre-wrap;word-break:break-word;margin-top:2px}
.tline{font:13px/1.45 ui-monospace,monospace;color:var(--mut);padding:2px 0 2px 8px;
border-left:2px solid var(--line);margin:4px 0;display:flex;gap:4px}
.tline code{word-break:break-all}
.tline.cmd{color:var(--ok)}.tline.edit{color:var(--warn)}.tline.read{color:var(--mut)}
.sys{font-size:13px;color:var(--warn);margin:4px 0}
#live .bub{background:var(--card)}
.chatfoot{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px}
.btn-sm{font-size:13px;padding:4px 10px}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.c-waiting{color:var(--warn)}.c-running{color:var(--acc)}.c-done{color:var(--ok)}
.c-failed,.c-safety_stop,.c-refused{color:var(--bad)}.c-stopped,.c-interrupted{color:var(--mut)}
.pill.cs-waiting{color:var(--warn)}.pill.cs-running{color:var(--acc)}.pill.cs-done{color:var(--ok)}
.pill.cs-failed,.pill.cs-safety_stop,.pill.cs-refused{color:var(--bad)}
.pill.cs-stopped,.pill.cs-interrupted,.pill.cs-stopping{color:var(--mut)}
/* Job D: phones - one column, the log and the box big enough for a thumb, the chat list as cards
   (the labels come from CSS, like the jobs table in phonelayout.py). */
@media (max-width:%dpx){
.chatlog{max-height:46vh;padding:6px}
.bub{padding:8px}
#chattext{min-height:110px}
.chatfoot{gap:6px}
.chatfoot button{min-height:%dpx}
#chatsend,#chatstop button{min-height:%dpx}
table.chats thead{display:none}
table.chats,table.chats tbody,table.chats tr,table.chats td{display:block;width:auto;min-width:0;max-width:none}
table.chats tr{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px;margin-bottom:8px}
table.chats td{border:0;padding:2px 0}
table.chats td::before{display:block;font-size:11px;font-weight:600;color:var(--mut);
text-transform:uppercase;letter-spacing:.04em}
@@COLS@@
}</style>""" % (phonelayout.PHONE_MAX, phonelayout.TAP, phonelayout.TAP)


def col_rules():
    """One labelled line per chats-table column, on a phone (like phonelayout.col_rules)."""
    return "\n".join('table.chats td:nth-child(%d)::before{content:"%s"}' % (n, name)
                     for n, name in enumerate(CHAT_COLS, 1))


CSS = CSS.replace("@@COLS@@", col_rules())
