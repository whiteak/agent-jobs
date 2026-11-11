"""Agent Jobs upgrades 1-3 wired into the job board (added 2026-11-11 by Claude).

Kept in its own module (like jailcfg.py) so jobboard.py only needs small hooks:

  * settings          ~/agent-sandbox/state/agentjobs-settings.json (fix rounds + watchdog
                      thresholds; written with the module defaults on first start)
  * make_wrap()       runs a project command (checks, git, the fake agent) in the SAME
                      offline sandbox local agents use: jail/run-local.sh sandbox-run
                      (jail/podman-local.sh flags, only /work/<folder> mounted, no network).
                      Host paths under the project are mapped to /work/<folder>/...
  * EventLog          writes the board's own events ({"type":"agentjobs",...}) into the
                      job's events.jsonl, but only between agent runs (never interleaved
                      with the agent's own output)
  * Feeder / Monitor  parse each mode's --stream log (Claude stream-json and Qwen's) into
                      the watchdog; replaces the old after-result watchdog (now finds the
                      result line whatever the key order)
  * checkpoint / checks helpers and the HTML for the job page

The agentjobs_upgrades package holds the tested logic (checks, guard, verify, safegit,
checkpoints, watchdog, fakeagent). Standard library only.
"""
import html, json, os, re, secrets, shlex, shutil, signal, subprocess, sys, threading, time
from dataclasses import asdict
from pathlib import Path

from agentjobs_upgrades import checks as ck
from agentjobs_upgrades import checkpoints as cpm
from agentjobs_upgrades import watchdog as wdm
from agentjobs_upgrades.safegit import SafeGit
import dashboard   # U07: timeline
import visioncheck   # U06: vision checks (screenshot the app, ask the model)

WEB = Path(__file__).resolve().parent
JAIL_DIR = WEB.parent / "jail"
RUN_LOCAL = JAIL_DIR / "run-local.sh"
FAKEAGENT = WEB / "agentjobs_upgrades" / "fakeagent.py"
# Tests only: JOBBOARD_JAIL=host runs checks, git and the fake agent directly (temp folders).
HOST_MODE = os.environ.get("JOBBOARD_JAIL") == "host"
# Hidden "fake" mode (scenario-driven fake agent, no model) for tests and the 8091 copy.
FAKE_ON = os.environ.get("JOBBOARD_FAKE") == "1"
FAKE_MODE = "fake"

CHECKS_MAX = 20
CHECK_CMD_MAX = 2000
FIX_ROUNDS_MAX = 10


# ------------------------------------------------------------------ settings

def _wd_defaults():
    d = asdict(wdm.WatchdogSettings())
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()}


DEFAULTS = {"fix_rounds": 3, "after_result_grace_s": 120, "watchdog": _wd_defaults(),
            "watchdog_tick_s": 15, "model_ping_s": 30,
            # U05: best-of-N for hard jobs (attempts 2-4, stop early unless marked hard,
            # and start fresh attempts by hand only - auto_attempts = do it for a job that
            # failed after its fix rounds)
            "attempts": 3, "stop_early": True, "auto_attempts": False}


def load_settings(path, write_defaults=True):
    """Settings with the module defaults filled in. The file is created with the
    defaults the first time, so the thresholds are easy to find and edit."""
    path = Path(path)
    data = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except FileNotFoundError:
        if write_defaults:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(DEFAULTS, indent=1) + "\n", encoding="utf-8")
            except OSError:
                pass
    except (OSError, ValueError):
        data = {}
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in data.items():
        if k == "watchdog" and isinstance(v, dict):
            out["watchdog"].update({a: b for a, b in v.items() if a in out["watchdog"]})
        elif k in out and isinstance(v, bool) and isinstance(out[k], bool):   # U05: on/off settings
            out[k] = v
        elif k in out and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
    return out


def watchdog_settings(cfg):
    kw = {}
    for k, v in (cfg.get("watchdog") or {}).items():
        if k in ("allowed_urls", "blocked_ports"):
            v = tuple(v)
        kw[k] = v
    try:
        return wdm.WatchdogSettings(**kw)
    except TypeError:
        return wdm.WatchdogSettings()


# ------------------------------------------------------------------ the sandbox wrapper

def map_paths(cmd, root, ctr):
    """Replace the project's host path (as given and resolved) with its container path."""
    for r in {str(root), str(Path(root).resolve())}:
        cmd = re.sub(re.escape(r) + r"(?=/|$|[\s'\"])", ctr, cmd)
    return cmd


def make_wrap(root, folder, prefix):
    """checks/safegit Wrap: (host path, command) -> argv. The command runs in the offline
    local-job sandbox (run-local.sh sandbox-run), cd'ed to the mapped path; a path outside
    the project folder is refused. Each run gets its own container name
    <prefix>-<random>, so stop_job_containers() can stop it."""
    root = Path(root)
    ctr = f"/work/{folder}"

    def wrap(path, cmd):
        p = Path(path)
        if HOST_MODE:
            return ["bash", "-c", cmd]
        try:
            rel = p.resolve().relative_to(root.resolve())
        except ValueError:
            raise ValueError(f"refusing to run outside the project folder: {p}")
        cdir = ctr if str(rel) == "." else f"{ctr}/{rel.as_posix()}"
        script = (f"cd {shlex.quote(cdir)} || exit 2; export PYTHONPYCACHEPREFIX=/tmp/.pycache; "
                  + map_paths(cmd, root, ctr))
        return [str(RUN_LOCAL), "sandbox-run", str(root), folder,
                f"{prefix}-{secrets.token_hex(3)}", "--", "-lc", script]
    return wrap


def stop_job_containers(jid, kill=False):
    """Stop (or kill) the check / git / checkpoint containers of a job."""
    if HOST_MODE:
        return
    try:
        r = subprocess.run(["podman", "ps", "-q", "--filter", f"name=^agent(check|ckpt|git|shot)-{jid}-"],
                           capture_output=True, text=True, timeout=30)
        ids = r.stdout.split()
        if ids:
            subprocess.run(["podman", "kill" if kill else "stop"] + ([] if kill else ["-t", "5"]) + ids,
                           capture_output=True, timeout=60)
    except Exception:
        pass


# ------------------------------------------------------------------ checks

def form_checks(text):
    """The new-job form's Checks box: one command per line -> check list (or None)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()][:CHECKS_MAX]
    out = []
    for i, cmd in enumerate(lines):
        cmd = cmd[:CHECK_CMD_MAX]
        name = " ".join(cmd.split()[:4])[:40] or f"check {i + 1}"
        if any(c["name"] == name for c in out):
            name = f"{name} ({i + 1})"
        out.append({"name": name, "cmd": cmd, "timeout_s": ck.DEFAULT_TIMEOUT})
    return out or None


def checks_ok(v):
    """check_record helper: None, or a well-formed list of checks."""
    if v is None:
        return True
    if not isinstance(v, list) or not 1 <= len(v) <= CHECKS_MAX:
        return False
    for c in v:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not c["name"] \
                or len(c["name"]) > 80:
            return False
        t = c.get("timeout_s", ck.DEFAULT_TIMEOUT)
        if not isinstance(t, int) or isinstance(t, bool) or not 1 <= t <= 7200:
            return False
        if c.get("type", "command") == "vision":       # U06: a screenshot + model check (no cmd)
            if not visioncheck.check_ok(c):
                return False
            continue
        if c.get("type", "command") != "command" or not isinstance(c.get("cmd"), str) \
                or not c["cmd"].strip() or len(c["cmd"]) > CHECK_CMD_MAX or "\x00" in c["cmd"]:
            return False
    return True


def job_checks(m):
    """The job's frozen checks (form or AGENT_CHECKS.json as it was at job start), or
    None = read AGENT_CHECKS.json / detect at gate time. U06: a vision check keeps its
    type and its extra fields (open / spec / steps / size / mode), since it has no cmd."""
    if not m.get("checks"):
        return None
    known = {"name", "cmd", "timeout_s", "type"}
    out = []
    for c in m["checks"]:
        out.append(ck.Check(str(c.get("name") or ""), str(c.get("cmd", "")),
                            int(c.get("timeout_s", ck.DEFAULT_TIMEOUT)),
                            type=str(c.get("type", "command")),
                            extra={k: v for k, v in c.items() if k not in known}))
    return out


def freeze_checks(m, root):
    """At job START (before the agent runs) copy AGENT_CHECKS.json into the record, so
    the agent cannot change what it will be checked with."""
    if m.get("checks"):
        return
    try:
        checks, src = ck.load_checks(root)
    except Exception:
        return
    if src == "file" and checks:
        lst = []
        for c in checks[:CHECKS_MAX]:
            if c.type == "command" and c.cmd.strip():
                lst.append({"name": c.name[:80], "cmd": c.cmd[:CHECK_CMD_MAX], "timeout_s": int(c.timeout_s)})
            elif c.type == "vision":                   # U06
                frozen = visioncheck.frozen(c)
                if frozen:
                    lst.append(frozen)
        if lst and checks_ok(lst):
            m["checks"] = lst
            m["checks_source"] = "AGENT_CHECKS.json at job start"


# ------------------------------------------------------------------ board events

class EventLog:
    """The board's own lines in events.jsonl. While an agent run is writing the same
    file, lines are held and written right after it ends (no interleaving)."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.buf = []
        self.holding = False
        self.history = []

    def __call__(self, kind, text, data=None, tone=""):
        """ev("checks", "...") is the same as ev.emit(...) - what the 7a tests and
        gate callbacks expect."""
        return self.emit(kind, text, data, tone)

    def emit(self, kind, text, data=None, tone=""):
        rec = {"type": "agentjobs", "kind": kind, "text": str(text)[:4000], "time": time.time()}
        if tone:
            rec["tone"] = tone
        if data:
            rec["data"] = data
        tl = dashboard.TL_KIND.get(kind)      # U07: also on the job's timeline
        if tl:
            dashboard.tl_add(self.path.parent, tl, rec["text"], data)
        with self.lock:
            self.history.append(rec)
            self.buf.append(rec)
            if not self.holding:
                self._flush()

    def hold(self):
        with self.lock:
            self.holding = True

    def release(self):
        with self.lock:
            self.holding = False
            self._flush()

    def _flush(self):
        if not self.buf:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                for r in self.buf:
                    f.write(json.dumps(r, default=str) + "\n")
            self.buf = []
        except OSError:
            pass


TONE = {"green": "ok", "red": "bad", "yellow": "warn", "orange": "warn2"}


def gate_emit(ev):
    """VerifyGate / CheckpointManager emit(kind, text, data) -> EventLog with a tone."""
    def emit(kind, text, data):
        tone = ""
        if kind == "checks":
            tone = TONE.get((data or {}).get("colour"), "")
        elif kind in ("needs_review", "rollback"):
            tone = "warn2"
        elif kind == "checkpoint":
            tone = "ok"
        ev.emit(kind, text, {k: v for k, v in (data or {}).items() if k != "issues"} or None, tone)
        if kind == "needs_review":
            for i in (data or {}).get("issues", [])[:20]:
                ev.emit("guard", i, None, "warn2")
    return emit


# ------------------------------------------------------------------ log parsing -> watchdog

CMD_TOOLS = {"Bash", "run_shell_command", "shell", "bash"}
WRITE_TOOLS = {"Write", "write_file", "create_file"}
EDIT_TOOLS = {"Edit", "MultiEdit", "edit", "replace", "NotebookEdit"}
READ_TOOLS = {"Read", "read_file", "read_many_files"}


def _text_of(body):
    if isinstance(body, list):
        return "\n".join(str(x.get("text", "")) for x in body if isinstance(x, dict))
    return str(body or "")


def is_result_line(raw):
    """The agent's final result event, whatever the key order (Claude Code now puts
    "type" near the end). Returns the parsed dict or None."""
    raw = raw.strip()
    if not raw.startswith("{") or '"result"' not in raw:
        return None
    try:
        e = json.loads(raw)
    except ValueError:
        return None
    return e if isinstance(e, dict) and e.get("type") == "result" else None


class Feeder:
    """One parsed log line at a time -> watchdog observations. Covers Claude Code
    stream-json (local-claude, supervised, fake) and Qwen Code stream-json."""

    def __init__(self, wd):
        self.wd = wd
        self.pending = {}          # tool_use id -> command still running
        self.result = None         # {"ok": bool, "time": t}
        self.last_error = ""
        self.session = None

    def line(self, raw):
        raw = raw.strip()
        if not raw.startswith("{"):
            return
        try:
            e = json.loads(raw)
        except ValueError:
            return
        if not isinstance(e, dict):
            return
        if isinstance(e.get("session_id"), str):
            self.session = e["session_id"]
        t = e.get("type")
        if t == "result":
            self.result = {"ok": not e.get("is_error"), "time": time.time()}
            return
        if self.wd is None:
            return
        if t == "assistant":
            msg = e.get("message") or {}
            self._usage(msg.get("usage"))
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "tool_use":
                    self._tool(c.get("name") or "", c.get("input") if isinstance(c.get("input"), dict) else {},
                               c.get("id"))
                elif ct == "text" and str(c.get("text") or "").strip():
                    self.wd.on_assistant_message(str(c["text"]))
        elif t == "user":
            for c in (e.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    out = _text_of(c.get("content"))
                    if c.get("is_error"):
                        self.last_error = out[-3000:]
                    cmd = self.pending.pop(c.get("tool_use_id"), None)
                    if cmd is not None:
                        self.wd.on_command_result(cmd, out)
        elif t == "tool_progress":
            self.wd.on_activity()

    def _usage(self, u):
        if not isinstance(u, dict):
            return
        num = lambda k: u.get(k) if isinstance(u.get(k), int) else 0
        if "total_tokens" in u:          # Qwen: input_tokens already includes the cached part
            n = num("input_tokens")
        else:                            # Claude: input + cache read + cache creation
            n = num("input_tokens") + num("cache_read_input_tokens") + num("cache_creation_input_tokens")
        if n > 0:
            self.wd.on_tokens(n)

    def _tool(self, name, inp, tid):
        if name in CMD_TOOLS and isinstance(inp.get("command"), str):
            self.wd.on_command(inp["command"])
            if tid:
                self.pending[tid] = inp["command"]
            return
        path = inp.get("file_path") or inp.get("absolute_path") or inp.get("path") or inp.get("notebook_path")
        if name in WRITE_TOOLS and isinstance(path, str):
            self.wd.on_file_write(path, str(inp.get("content", "")))
        elif name in EDIT_TOOLS and isinstance(path, str):
            body = inp.get("new_string")
            if body is None:
                body = json.dumps(inp.get("edits") or inp.get("new_source") or "", sort_keys=True)
            self.wd.on_file_write(path, str(body))
        elif name in READ_TOOLS and isinstance(path, str):
            self.wd.on_file_read(path)
            self.wd.on_activity()
        else:
            self.wd.on_activity()


class Monitor:
    """Watches ONE agent process: reads its events.jsonl as it grows, feeds the
    watchdog, applies its actions, watches PLAN.md for checkpoints, and stops an
    agent that lingers after its final result (old watchdog, fixed)."""

    def __init__(self, events_path, offset, wd, *, local, stop, kill, pause, ping=None,
                 emit=None, plan_path=None, on_plan=None, grace=120, tick_s=15, ping_s=30,
                 poll_s=1.0, now=time.time):
        self.path = Path(events_path)
        self.offset = offset
        self.wd = wd
        self.feeder = Feeder(wd)
        self.local = local
        self.stop, self.kill, self.pause, self.ping = stop, kill, pause, ping
        self.emit = emit or (lambda *a, **k: None)
        self.plan_path = Path(plan_path) if plan_path else None
        self.on_plan = on_plan
        self.grace, self.tick_s, self.ping_s, self.poll_s = grace, tick_s, ping_s, poll_s
        self.now = now
        self.action = None            # watchdog Action that ended this run
        self.after_result = None      # True/False: stopped after its final result
        self.actions = []
        self._buf = b""
        self._plan_mt = None
        self._last_tick = self._last_ping = now()
        self._result_stop = False

    def _read(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(4_000_000)
        except OSError:
            return
        if not data:
            return
        self.offset += len(data)
        self._buf += data
        *lines, self._buf = self._buf.split(b"\n")
        for ln in lines:
            try:
                self.feeder.line(ln.decode("utf-8", "replace"))
            except Exception:
                pass

    def _plan(self):
        if not (self.plan_path and self.on_plan):
            return
        try:
            mt = self.plan_path.stat().st_mtime_ns
        except OSError:
            return
        if mt == self._plan_mt:
            return
        first = self._plan_mt is None
        self._plan_mt = mt
        try:
            text = self.plan_path.read_text(encoding="utf-8", errors="replace")[:200000]
            self.on_plan(text, first)
        except Exception:
            pass

    def _handle(self, acts, p):
        for a in acts:
            rec = {"time": time.time(), "kind": a.kind, "reason": a.reason}
            rec.update({k: str(v)[:500] for k, v in (a.data or {}).items()})
            if a.kind == "safety_stop" and not self.local:
                rec["kind"] = "safety_warning"
                rec["reason"] = a.reason + " (not stopped: supervised jobs may use the internet)"
                self.actions.append(rec)
                self.emit("watchdog", rec["reason"], {"kind": "safety_warning"}, "warn2")
                continue
            self.actions.append(rec)
            if a.kind == "safety_stop":
                what = a.data.get("command") or a.data.get("path") or ""
                self.emit("safety_stop", f"{a.reason}" + (f" - {what}" if what else ""),
                          {"command": str(what)[:500]}, "bad")
            else:
                self.emit("watchdog", f"Watchdog: {a.reason}", {"kind": a.kind},
                          "warn2" if a.kind != "resume" else "")
            if self.action is not None:
                continue
            if a.kind == "safety_stop":
                self.action = a
                self.kill(p)
            elif a.kind in ("restart", "restart_fresh", "stop"):
                self.action = a
                self.stop()
            elif a.kind == "pause":
                self.pause(True)
            elif a.kind == "resume":
                self.pause(False)

    def step(self, p):
        """One poll: read, plan, maybe tick, maybe the after-result stop."""
        self._read()
        self._plan()
        now = self.now()
        if self.wd is not None and (self.wd.has_urgent() or now - self._last_tick >= self.tick_s):
            self._last_tick = now
            if self.feeder.pending:
                self.wd.on_activity()       # a command is still running: that is work
            ok = None
            if self.ping and now - self._last_ping >= self.ping_s:
                self._last_ping = now
                try:
                    ok = bool(self.ping())
                except Exception:
                    ok = None
            self._handle(self.wd.tick(model_ok=ok, can_nudge=False), p)
        res = self.feeder.result
        if res and self.action is None and not self._result_stop and now - res["time"] > self.grace:
            self._result_stop = True
            self.after_result = res["ok"]
            self.emit("watchdog", f"The agent gave its final answer but did not exit within "
                                  f"{self.grace}s; stopped it", {"kind": "after_result"}, "")
            self.stop()

    def run(self, p):
        while p.poll() is None:
            time.sleep(self.poll_s)
            self.step(p)
        self._read()
        return p.returncode


def kill_now(p, container=None):
    """Safety stop: kill the container at once and the agent's whole process group."""
    if container and not HOST_MODE:
        try:
            subprocess.run(["podman", "kill", container], capture_output=True, timeout=30)
        except Exception:
            pass
    try:
        wdm.kill_process_group(os.getpgid(p.pid), grace_s=2.0, proc=p)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def pause_container(container, on, pid=None):
    if HOST_MODE:
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGSTOP if on else signal.SIGCONT)
            except OSError:
                pass
        return
    try:
        subprocess.run(["podman", "pause" if on else "unpause", container], capture_output=True, timeout=30)
    except Exception:
        pass


# ------------------------------------------------------------------ fake mode

def fake_cmd(m, root, folder, scenario_text, rnd, resume):
    """argv for the hidden fake agent. In the sandbox it runs like a check (offline,
    only this folder); its files go under .git/agentjobs/<id>/fake (or .agentjobs/<id>)."""
    jid = m["id"]
    base = (root / ".git" / "agentjobs" / jid / "fake") if (root / ".git").is_dir() else (root / ".agentjobs" / jid)
    base.mkdir(parents=True, exist_ok=True)
    (base / "scenario.json").write_text(scenario_text, encoding="utf-8")
    shutil.copy2(FAKEAGENT, base / "fakeagent.py")
    extra = ["--resume", resume] if resume else []
    if HOST_MODE:
        return [sys.executable, str(base / "fakeagent.py"), str(base / "scenario.json"), "--round", str(rnd),
                "--format", "stream-json"] + extra, str(root)
    cb = map_paths(str(base), root, f"/work/{folder}")
    script = (f"cd /work/{shlex.quote(folder)} && exec python3 {shlex.quote(cb)}/fakeagent.py "
              f"{shlex.quote(cb)}/scenario.json --round {int(rnd)} --format stream-json "
              + " ".join(shlex.quote(x) for x in extra))
    return [str(RUN_LOCAL), "sandbox-run", str(root), folder, "agentjob-" + jid, "--", "-c", script], None


# ------------------------------------------------------------------ checkpoints

def git_repo(root):
    return (Path(root) / ".git").is_dir()


def make_checkpoints(m, root, out_dir, ev):
    """CheckpointManager for a job, or None (not a git repo / broken state)."""
    if not git_repo(root):
        return None
    jid, folder = m["id"], m["folder"]
    git = SafeGit(root, wrap=make_wrap(root, folder, f"agentgit-{jid}"), timeout=300)
    cwrap = make_wrap(root, folder, f"agentckpt-{jid}")

    def run_checks_in(d):
        return ck.run_all(d, Path(out_dir) / f"ckpt-{int(time.time() * 1000)}", cwrap, checks=job_checks(m))

    return cpm.CheckpointManager(jid, root, Path(root) / ".git" / "agentjobs" / jid,
                                 run_checks_in=run_checks_in, git=git, emit=gate_emit(ev))


def git_for(m, root):
    return SafeGit(root, wrap=make_wrap(root, m["folder"], f"agentgit-{m['id']}"), timeout=300)


def handover(m, root, last_error=""):
    """watchdog.handover_message filled in from the project (git runs in the sandbox)."""
    plan = ""
    try:
        plan = (Path(root) / "PLAN.md").read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        pass
    log = stat = ""
    if git_repo(root):
        g = git_for(m, root)
        try:
            log = g.run("log", "--oneline", "-30", check=False)
            base = m.get("git_start")
            stat = g.run("diff", "--stat", base, check=False) if base else g.run("diff", "--stat", check=False)
        except Exception:
            pass
    return wdm.handover_message(m["job"], plan, log, stat, last_error)


# ------------------------------------------------------------------ status + HTML

LABELS = {"passed": None, "unverified": "Done – not verified", "needs_review": "Needs review",
          "stuck": "Stuck – needs a look", "failed": "Checks failed", "aborted": None}


def overlay_status(s, m):
    """Add verify / safety-stop / phase info to a jobstatus dict (in place)."""
    if m.get("status") == "safety_stop":
        s["label"] = "Safety stop"
        s["tone"] = "red"
        s["fail_reason"] = m.get("fail_note") or "Safety stop"
    phase = m.get("phase")
    if phase and m.get("status") in ("running", "stopping"):
        s["state"] = "WORKING"
        s["label"] = "Working"
        s["current_action"] = phase
    v = m.get("verify") or {}
    b = v.get("banner")
    if b and s["state"] in ("FINISHED", "FAILED") and m.get("status") != "safety_stop":
        s["verify"] = b
        s["tone"] = b.get("colour", "")
        lab = b.get("text") if v.get("status") == "passed" else LABELS.get(v.get("status"))
        if lab:
            s["label"] = lab[:60]
        if s["state"] == "FAILED" and v.get("status") in ("failed", "stuck"):
            s["fail_reason"] = b.get("text")
        if v.get("timeline"):
            s["verify_timeline"] = v["timeline"]
    return s


def esc(x):
    return html.escape(str(x if x is not None else ""), quote=True)


CSS = """<style>
.t-green{color:var(--ok)!important}.t-red{color:var(--bad)!important}.t-yellow{color:var(--warn)!important}.t-orange{color:#d9631e!important}
.banner.t-green{border-left-color:var(--ok)}.banner.t-red{border-left-color:var(--bad)}.banner.t-yellow{border-left-color:var(--warn)}.banner.t-orange{border-left-color:#d9631e}
.vline{font-weight:600;margin:6px 0 0}
.feed .gate{font-weight:600}.feed .gate.ok{color:var(--ok)}.feed .gate.bad{color:var(--bad)}.feed .gate.warn{color:var(--warn)}.feed .gate.warn2{color:#d9631e}
.feed .gate.bad{border:2px solid var(--bad);border-radius:7px;padding:8px}
td.c-ok{color:var(--ok)}td.c-bad{color:var(--bad)}
img.shot{width:230px;max-width:46vw;border:1px solid var(--line);border-radius:6px;margin:4px 8px 0 0;vertical-align:top;background:#fff}
td.shots{padding-top:2px}ul.issues{margin:4px 0 0;padding-left:20px}
</style>"""


def verify_line_html(s):
    b = s.get("verify")
    if not b:
        return '<div class="vline" id="bverify" hidden></div>'
    return f'<div class="vline t-{esc(b.get("colour"))}" id="bverify">{esc(b.get("text"))}</div>'


def _vision_row(i, r, jid, src):
    """One screenshot check: verdict, the screenshots, what the model saw wrong."""
    v = r.get("verdict") or "inconclusive"
    cls = "c-ok" if r.get("ok") else "c-bad"
    mark = "&#10003;" if r.get("ok") else ("&#63;" if v == "inconclusive" else "&#10007;")
    detail = str(r.get("cmd") or "vision") + " &middot; " + esc(r.get("mode", "advisory"))
    if isinstance(r.get("asked"), int):
        detail += " &middot; asked " + str(int(r["asked"])) + "x"
    if r.get("url"):
        detail += " &middot; " + esc(r["url"])[:80]
    rows = (f'<tr><td class="{cls}">{mark} {esc(r.get("name"))}</td><td class="hint">{detail}</td>'
            f'<td class="{cls}">{esc(visioncheck.WORD.get(v, v))}</td><td>{esc(r.get("seconds"))} s</td></tr>')
    shots = [str(s) for s in (r.get("screenshots") or []) if str(s)]
    if shots:
        if jid and src:
            pics = "".join(
                f'<a href="/api/shot/{esc(jid)}?src={esc(src)}&amp;n={i}&amp;i={k}" target="_blank" '
                f'title="Screenshot {k + 1} - click for full size">'
                f'<img class="shot" src="/api/shot/{esc(jid)}?src={esc(src)}&amp;n={i}&amp;i={k}" '
                f'alt="screenshot {k + 1}"></a>' for k in range(len(shots)))
            rows += f'<tr><td colspan=4 class="shots">{pics}</td></tr>'
        else:
            rows += ('<tr><td colspan=4 class="hint">screenshots: '
                     + ", ".join(esc(Path(s).name) for s in shots) + "</td></tr>")
    if r.get("issues"):
        rows += ('<tr><td colspan=4><ul class="issues">'
                 + "".join(f"<li>{esc(x)}</li>" for x in list(r["issues"])[:20]) + "</ul></td></tr>")
    return rows


def _res_rows(results, jid=None, src=None):
    rows = ""
    for i, r in enumerate(results or []):
        if r.get("type") == "vision":                     # U06: a screenshot check
            rows += _vision_row(i, r, jid, src)
            continue
        counts = " / ".join(f"{r.get(k)} {k}" for k in ("passed", "failed", "skipped") if r.get(k) is not None)
        cls = "c-ok" if r.get("ok") else "c-bad"
        rows += (f'<tr><td class="{cls}">{"&#10003;" if r.get("ok") else "&#10007;"} {esc(r.get("name"))}</td>'
                 f'<td><code>{esc(r.get("cmd"))}</code></td><td>{esc(counts) or "-"}</td>'
                 f'<td>{esc(r.get("seconds"))} s</td></tr>')
        if not r.get("ok") and r.get("first_error"):
            rows += f'<tr><td colspan=4><pre class="bad">{esc(r["first_error"][:3000])}</pre></td></tr>'
    return rows


def _check_hint(c):
    """One frozen check, as a short piece of HTML (a screenshot check has no command)."""
    if c.get("type") == "vision":
        return "<code>screenshot of %s</code>" % esc(str(c.get("open") or "")[:60])
    return f"<code>{esc(c.get('cmd', ''))}</code>"


def checks_card_html(m, jid, can_recheck):
    v = m.get("verify")
    fixed = m.get("checks")
    rech = m.get("recheck")
    if not v and not fixed and not rech and not m.get("verify_off"):
        return ""
    parts = []
    if v and v.get("timeline"):
        parts.append(f'<p><b>{esc(v["timeline"])}</b></p>')
    if v and v.get("status") == "aborted":
        parts.append(f'<p class="mut">Checks stopped: {esc(v.get("reason", ""))}</p>')
    if m.get("verify_off"):
        parts.append('<p class="mut">Don\'t verify was ticked for this job.</p>')
    if fixed:
        src = m.get("checks_source") or "the new-job form"
        parts.append(f'<p class="hint">Checks from {esc(src)}: ' +
                     ", ".join(_check_hint(c) for c in fixed) + "</p>")
    for rnd in (v or {}).get("rounds", []):
        summ = rnd.get("summary") or {}
        title = "After the agent finished" if rnd["round"] == 0 else f"After fix round {rnd['round']}"
        colour, text = ck.banner_text(summ)
        parts.append(f'<h3 style="font-size:14px;margin:12px 0 4px">{esc(title)}: <span class="t-{colour}">{esc(text)}</span></h3>')
        if summ.get("source") == "detected":
            parts.append('<p class="hint">Checks were detected from the project files.</p>')
        rows = _res_rows(summ.get("results"), jid, "round%d" % int(rnd.get("round") or 0))
        if rows:
            parts.append(f'<div class="tbl"><table>{rows}</table></div>')
        if rnd.get("issues"):
            parts.append('<p class="t-orange"><b>Tests changed in a suspicious way:</b></p><ul>' +
                         "".join(f"<li>{esc(i)}</li>" for i in rnd["issues"]) + "</ul>")
    if rech:
        colour, text = ck.banner_text(rech.get("summary") or {})
        when = time.strftime("%d %b %H:%M", time.localtime(rech.get("time", 0)))
        if rech.get("running"):
            parts.append('<p><b>Running the checks again...</b> (reload in a moment)</p>')
        else:
            parts.append(f'<h3 style="font-size:14px;margin:12px 0 4px">Checked again at {when}: '
                         f'<span class="t-{colour}">{esc(text)}</span></h3>')
            rows = _res_rows((rech.get("summary") or {}).get("results"), jid, "recheck")
            if rows:
                parts.append(f'<div class="tbl"><table>{rows}</table></div>')
    btn = ""
    if can_recheck:
        btn = (f'<form method="post" action="/recheck/{esc(jid)}" style="margin-top:10px">'
               f'<button class="ghost2">Run checks again</button></form>')
    return f'<div class="card"><h2>Checks</h2>{"".join(parts)}{btn}</div>'


def checkpoints_card_html(m, jid, can_act):
    cp = m.get("checkpoints")
    note = m.get("checkpoints_note")
    if not cp and not note:
        return ""
    rows = ""
    for c in (cp or {}).get("checkpoints", []):
        when = time.strftime("%d %b %H:%M", time.localtime(c.get("time", 0)))
        btn = ""
        if can_act:
            btn = (f'<form method="post" action="/rollback/{esc(jid)}" style="display:inline" '
                   f'onsubmit="return confirm(\'Roll the project folder back to checkpoint {int(c["n"])}? '
                   f'The current state is saved on a discarded branch first.\')">'
                   f'<input type="hidden" name="n" value="{int(c["n"])}"><button class="ghost2">Roll back to here</button></form>')
        rows += (f'<tr><td>{int(c["n"])}</td><td>{esc(c.get("step"))}</td><td>{when}</td>'
                 f'<td>{esc(c.get("passed"))} passing</td><td><code>{esc(str(c.get("commit", ""))[:10])}</code></td><td>{btn}</td></tr>')
    body = ""
    if rows:
        body += ('<div class="tbl"><table><thead><tr><th>#</th><th>Step</th><th>Time</th><th>Tests</th>'
                 f'<th>Commit</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>')
    elif cp is not None:
        body += '<p class="mut">No checkpoints yet (one is saved each time a PLAN.md step is ticked and its checks pass).</p>'
    if (cp or {}).get("discarded"):
        body += ('<p class="hint">Rolled-back attempts are saved on: ' +
                 ", ".join(f"<code>{esc(b)}</code>" for b in cp["discarded"]) + "</p>")
    if (cp or {}).get("rollbacks"):
        body += f'<p class="hint">Automatic rollbacks: {int(cp["rollbacks"])}</p>'
    if note:
        body += f'<p class="mut">{esc(note)}</p>'
    if cp is not None and can_act:
        body += (f'<form method="post" action="/ckpt-cleanup/{esc(jid)}" style="margin-top:8px">'
                 f'<button class="ghost2">Clean up</button> <span class="hint">deletes checkpoint refs and discarded '
                 f'branches older than 14 days</span></form>')
    return f'<div class="card"><h2>Checkpoints</h2>{body}</div>'


def watchdog_card_html(m):
    acts = m.get("watchdog") or []
    if not acts:
        return ""
    lis = ""
    for a in acts[-30:]:
        when = time.strftime("%H:%M:%S", time.localtime(a.get("time", 0)))
        cls = "bad" if a.get("kind") == "safety_stop" else ""
        extra = a.get("command") or a.get("path") or ""
        lis += (f'<li class="{cls}">{when} <b>{esc(a.get("kind"))}</b>: {esc(a.get("reason"))}'
                + (f' <code>{esc(extra)}</code>' if extra else "") + "</li>")
    return f'<div class="card"><h2>Watchdog</h2><ul>{lis}</ul></div>'


def new_job_fields(default_rounds):
    return (f'<label>Checks <span class="hint" style="font-weight:400">(optional, one command per line; '
            f'run in the sandbox after the agent finishes. Empty = AGENT_CHECKS.json or detect)</span></label>'
            f'<textarea name="checks" style="min-height:60px" placeholder="python3 -m unittest discover -s tests"></textarea>'
            f'<div class="brow" style="margin-top:6px"><label style="font-weight:400;margin:0">'
            f'<input type="checkbox" name="noverify"> Don\'t verify (for jobs with nothing to test, like docs)</label>'
            f'<label style="font-weight:400;margin:0">Fix rounds <input type="number" name="fixrounds" min="0" '
            f'max="{FIX_ROUNDS_MAX}" value="{int(default_rounds)}" style="width:70px;display:inline-block"></label></div>')


# ------------------------------------------------------------------ review bundle

def _vision_md(r):
    """Markdown lines for one screenshot check: verdict, what the model saw wrong, and the
    screenshot file names (so whoever reviews the bundle can ask for the pictures)."""
    out = [f"  - screen check `{r.get('name')}`: {r.get('verdict')} ({r.get('mode', 'advisory')})"]
    for i in list(r.get("issues") or [])[:20]:
        out.append(f"    - seen on screen: {i}")
    for p in list(r.get("screenshots") or [])[:8]:
        out.append(f"    - screenshot: {Path(str(p)).name}")
    return out


def bundle_section(m):
    """Markdown for the review bundle: verify rounds, guard issues, checkpoints, watchdog."""
    out = []
    v = m.get("verify")
    if v or m.get("verify_off"):
        out.append("## Verify gate")
        if m.get("verify_off"):
            out.append("- Don't verify was ticked")
        if v:
            b = v.get("banner") or {}
            out.append(f"- result: {v.get('status')} - {b.get('text', '')}")
            if v.get("timeline"):
                out.append(f"- timeline: {v['timeline']}")
            for rnd in v.get("rounds", []):
                summ = rnd.get("summary") or {}
                tag = "agent finished" if rnd["round"] == 0 else f"fix round {rnd['round']}"
                out.append(f"- {tag}: {ck.banner_text(summ)[1]}")
                for r in summ.get("results", []):
                    if r.get("type") == "vision":                 # U06
                        out += _vision_md(r)
                        continue
                    counts = ", ".join(f"{r.get(k)} {k}" for k in ("passed", "failed", "skipped") if r.get(k) is not None)
                    out.append(f"  - `{r.get('cmd')}` exit {r.get('exit_code')}" + (f" ({counts})" if counts else ""))
                for i in rnd.get("issues") or []:
                    out.append(f"  - CHEATING GUARD: {i}")
        if m.get("recheck") and (m["recheck"].get("summary")):
            summ = m["recheck"]["summary"]
            out.append(f"- checked again: {ck.banner_text(summ)[1]}")
            for r in summ.get("results", []):
                if r.get("type") == "vision":
                    out += _vision_md(r)
    cp = m.get("checkpoints")
    if cp or m.get("checkpoints_note"):
        out.append("\n## Checkpoints")
        for c in (cp or {}).get("checkpoints", []):
            out.append(f"- {c['n']}: {c.get('step')} - {c.get('passed')} passing - {str(c.get('commit', ''))[:10]}")
        if (cp or {}).get("rollbacks"):
            out.append(f"- automatic rollbacks: {cp['rollbacks']}")
        for b in (cp or {}).get("discarded", []):
            out.append(f"- discarded attempt saved on {b}")
        if m.get("checkpoints_note"):
            out.append(f"- {m['checkpoints_note']}")
    if m.get("watchdog"):
        out.append("\n## Watchdog")
        for a in m["watchdog"][-30:]:
            extra = a.get("command") or a.get("path") or ""
            out.append(f"- {a.get('kind')}: {a.get('reason')}" + (f" (`{extra}`)" if extra else ""))
    return "\n".join(out).strip()
