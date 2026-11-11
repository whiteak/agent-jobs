"""Upgrade 3: runaway watchdog and safety stop.

A pure rule engine: the job runner feeds it what it sees in the agent's log
(commands, command output, file writes, assistant messages, token usage) and
calls tick() every 15 s. tick() returns the actions to take. The runner does
the actual nudging / stopping, using kill_process_group() so no child is left.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

# ------------------------------------------------------------------ settings

@dataclass
class WatchdogSettings:
    idle_nudge_s: int = 15 * 60          # no tool call / file change → nudge
    idle_restart_after_nudge_s: int = 10 * 60
    max_restarts: int = 2                # no-progress / loop restarts per job
    loop_repeats: int = 3
    context_size: int = 262_144
    context_fraction: float = 0.85
    max_context_restarts: int = 3
    max_wall_s: int = 4 * 3600
    model_down_pause_s: int = 120
    allowed_urls: tuple = ("http://127.0.0.1:8080", "http://localhost:8080")
    blocked_ports: tuple = (8090, 8091)


@dataclass
class Action:
    kind: str            # nudge | restart | restart_fresh | stop | safety_stop | pause | resume
    reason: str
    message: str = ""
    data: dict = field(default_factory=dict)


NUDGE_MSG = "Stop planning. Take the smallest next step now and run it."
RESTART_MSG = ("You were restarted because you stopped making progress{why}. Read PLAN.md and "
               "git log, then continue with the next unticked step.")

# ------------------------------------------------------------------ safety rules

NET_PROGRAMS = {"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "ftp", "telnet",
                "socat", "aws", "ossutil", "ossutil64", "gsutil", "az", "gcloud", "rclone",
                "aria2c", "lftp", "smbclient", "tftp", "http", "https", "xh", "httpie"}
INSTALLERS = {"pip": "install", "pip3": "install", "npm": "install", "pnpm": "add", "yarn": "add",
              "apt": "install", "apt-get": "install", "snap": "install", "gem": "install",
              "cargo": "install", "go": "get", "conda": "install", "brew": "install"}
OFFLINE_FLAGS = ("--offline", "--no-index", "--prefer-offline")
GIT_NET = {"push", "fetch", "pull", "clone", "ls-remote", "submodule"}
SECRET_PATH_RE = re.compile(
    r"(~|\$HOME|\$\{HOME\}|/home/[^/\s'\"]+|/root)/("
    r"\.ssh|\.gnupg|\.aws|\.netrc|\.git-credentials|\.password-store|\.docker|\.kube"
    r"|\.local/share/keyrings|\.mozilla|\.config/(google-chrome|chromium|BraveSoftware|"
    r"Code|gh|git|rclone|qwen)|\.qwen|\.claude|\.cline|snap/(firefox|chromium)|"
    r"basalt_config|agent-sandbox/(web|state/secrets))"
    r"|/etc/(shadow|sudoers)|/proc/\d+/environ")
URL_RE = re.compile(r"\b(?:https?|ftp|wss?)://[^\s'\"<>)]+", re.I)
HOST_PORT_RE = re.compile(r"\b(?:127\.0\.0\.1|localhost|0\.0\.0\.0):(\d{2,5})\b")
SEPARATORS = re.compile(r"\|\||&&|[;|&\n]|\$\(|`")


def _segments(cmd: str):
    for seg in SEPARATORS.split(cmd):
        seg = seg.strip().lstrip("({").strip()
        if not seg:
            continue
        try:
            words = shlex.split(seg, posix=True)
        except ValueError:
            words = seg.split()
        # skip env assignments and wrappers
        while words and (re.match(r"^\w+=", words[0]) or words[0] in ("sudo", "env", "nohup", "time",
                                                                       "timeout", "nice", "exec", "command")):
            if words[0] == "timeout" and len(words) > 1:
                words = words[2:]
            else:
                words = words[1:]
        if words:
            yield words


def safety_reason(cmd: str, settings: WatchdogSettings = WatchdogSettings()) -> Optional[str]:
    """Why this command must stop the job, or None if it looks fine."""
    for url in URL_RE.findall(cmd):
        if any(url.lower().startswith(a) for a in settings.allowed_urls):
            continue
        m = re.match(r"^\w+://(127\.0\.0\.1|localhost|\[::1\])(?::(\d+))?", url, re.I)
        if m and int(m.group(2) or 80) not in settings.blocked_ports:
            continue                     # the project's own local test server is fine
        return f"tried to reach {url}"
    for port in HOST_PORT_RE.findall(cmd):
        if int(port) in settings.blocked_ports:
            return f"tried to reach the Agent Jobs page (port {port})"
    m = SECRET_PATH_RE.search(cmd)
    if m:
        return f"touched a private location ({m.group(0)})"
    for words in _segments(cmd):
        prog = os.path.basename(words[0])
        if prog in NET_PROGRAMS:
            # curl/wget to the model server only is fine (URL check above passed)
            if prog in ("curl", "wget", "http", "xh") and URL_RE.search(" ".join(words)):
                continue
            return f"ran a network tool ({prog})"
        if prog in ("python", "python3") and len(words) > 2 and words[1] == "-m" and words[2] == "pip":
            words = ["pip"] + words[3:]
            prog = "pip"
        if prog in INSTALLERS and len(words) > 1 and words[1] in (INSTALLERS[prog], "i", "ci", "add"):
            if prog == "npm" and words[1] == "ci" and "--offline" in words:
                continue
            if not any(f in words for f in OFFLINE_FLAGS):
                return f"tried to install packages ({prog} {words[1]})"
        if prog == "git" and len(words) > 1:
            sub = next((w for w in words[1:] if not w.startswith("-") and "=" not in w), "")
            if sub in GIT_NET:
                return f"tried to use git over the network (git {sub})"
            if sub == "remote" and any(w in ("add", "set-url") for w in words):
                return "tried to add a git remote"
    if re.search(r"\b(socket\.connect|urllib\.request|requests\.(get|post)|http\.client|"
                 r"fetch\(|XMLHttpRequest)|/dev/(tcp|udp)/", cmd) and not URL_RE.search(cmd):
        return "ran code that opens network connections"
    return None


def path_reason(path: str, project_dir: str) -> Optional[str]:
    """Writing/reading outside the project in a private place."""
    ap = os.path.abspath(os.path.expanduser(path))
    proj = os.path.abspath(project_dir)
    if ap == proj or ap.startswith(proj + os.sep):
        return None
    if SECRET_PATH_RE.search(ap):
        return f"touched a private location ({ap})"
    return None


# ------------------------------------------------------------------ engine

def _h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()


class Watchdog:
    def __init__(self, settings: Optional[WatchdogSettings] = None, project_dir: str = ".",
                 now: Callable[[], float] = time.time):
        self.s = settings or WatchdogSettings()
        self.project_dir = project_dir
        self.now = now
        self.started = now()
        self.last_progress = self.started
        self.nudged_at: Optional[float] = None
        self.restarts = 0
        self.context_restarts = 0
        self.tokens = 0
        self.model_down_since: Optional[float] = None
        self.paused = False
        self.stopped = False
        self._cmds = deque(maxlen=self.s.loop_repeats)
        self._msgs = deque(maxlen=self.s.loop_repeats)
        self._writes: dict[str, deque] = {}
        self._pending: list[Action] = []
        self.history: list[dict] = []

    # ---- observations ------------------------------------------------
    def _progress(self):
        self.last_progress = self.now()
        self.nudged_at = None

    def on_command(self, cmd: str) -> None:
        self._progress()
        why = safety_reason(cmd, self.s)
        if why:
            self._pending.append(Action("safety_stop", f"Safety stop: {why}", data={"command": cmd}))

    def on_command_result(self, cmd: str, output: str) -> None:
        self._cmds.append(_h(cmd + "\0" + output))
        if len(self._cmds) == self._cmds.maxlen and len(set(self._cmds)) == 1:
            self._cmds.clear()
            self._loop(f"ran the same command {self.s.loop_repeats} times with the same result ({cmd[:80]})")

    def on_file_write(self, path: str, content: str | bytes = "") -> None:
        self._progress()
        why = path_reason(path, self.project_dir)
        if why:
            self._pending.append(Action("safety_stop", f"Safety stop: {why}", data={"path": path}))
            return
        h = hashlib.sha1(content if isinstance(content, bytes) else content.encode()).hexdigest()
        q = self._writes.setdefault(path, deque(maxlen=self.s.loop_repeats))
        q.append(h)
        if len(q) == q.maxlen and len(set(q)) == 1:
            q.clear()
            self._loop(f"wrote the same content to {os.path.basename(path)} {self.s.loop_repeats} times")

    def on_file_read(self, path: str) -> None:
        why = path_reason(path, self.project_dir)
        if why:
            self._pending.append(Action("safety_stop", f"Safety stop: {why}", data={"path": path}))

    def on_assistant_message(self, text: str) -> None:
        t = " ".join(text.split())
        if len(t) < 20:
            return
        self._msgs.append(_h(t))
        if len(self._msgs) == self._msgs.maxlen and len(set(self._msgs)) == 1:
            self._msgs.clear()
            self._loop("repeated the same message")

    def on_tokens(self, used: int) -> None:
        self.tokens = used
        if used >= self.s.context_size * self.s.context_fraction:
            if self.context_restarts >= self.s.max_context_restarts:
                self._pending.append(Action("stop", "Stopped: kept filling the context window"))
            else:
                self.context_restarts += 1
                self._pending.append(Action("restart_fresh",
                                            f"Context {used:,} of {self.s.context_size:,} tokens: fresh session",
                                            data={"tokens": used}))
                self.tokens = 0

    def _loop(self, what: str) -> None:
        self._restart_or_stop(f"loop: {what}", f" (you {what})")

    def _restart_or_stop(self, reason: str, why: str = "") -> None:
        if self.restarts >= self.s.max_restarts:
            self._pending.append(Action("stop", f"Stopped: no progress ({reason})"))
        else:
            self.restarts += 1
            self._pending.append(Action("restart", reason, RESTART_MSG.format(why=why)))
        self._progress()

    # ---- periodic ----------------------------------------------------
    def tick(self, model_ok: Optional[bool] = None, can_nudge: bool = True) -> list[Action]:
        now = self.now()
        acts = self._pending
        self._pending = []
        if model_ok is False:
            self.model_down_since = self.model_down_since or now
            if not self.paused and now - self.model_down_since >= self.s.model_down_pause_s:
                self.paused = True
                acts.append(Action("pause", "Waiting for the model server"))
        elif model_ok is True:
            self.model_down_since = None
            if self.paused:
                self.paused = False
                self._progress()
                acts.append(Action("resume", "Model server is back"))
        if self.paused:
            return self._log(acts)
        if now - self.started >= self.s.max_wall_s:
            acts.append(Action("stop", "Stopped: time limit"))
        idle = now - self.last_progress
        if self.nudged_at is None and idle >= self.s.idle_nudge_s:
            if can_nudge:
                self.nudged_at = now
                acts.append(Action("nudge", f"No progress for {int(idle // 60)} min", NUDGE_MSG))
            else:
                self._restart_or_stop(f"no progress for {int(idle // 60)} min")
                acts.extend(self._pending)
                self._pending = []
        elif self.nudged_at is not None and now - self.nudged_at >= self.s.idle_restart_after_nudge_s:
            self._restart_or_stop(f"no progress for {int(idle // 60)} min, even after a nudge")
            acts.extend(self._pending)
            self._pending = []
        # a safety stop wins over everything else
        safety = [a for a in acts if a.kind == "safety_stop"]
        if safety:
            acts = safety[:1]
        if any(a.kind in ("stop", "safety_stop") for a in acts):
            self.stopped = True
        return self._log(acts)

    def _log(self, acts: list[Action]) -> list[Action]:
        for a in acts:
            self.history.append({"time": self.now(), "kind": a.kind, "reason": a.reason, **a.data})
        return acts


# ------------------------------------------------------------------ helpers

def handover_message(task: str, plan: str, git_log: str, diff_stat: str, last_error: str = "") -> str:
    parts = ["You are continuing a job in a fresh session because the previous one filled its context.",
             "", "## Task", task.strip(), "", "## PLAN.md", plan.strip() or "(missing)", "",
             "## Commits so far", git_log.strip() or "(none)", "", "## Changed files", diff_stat.strip() or "(none)"]
    if last_error:
        parts += ["", "## Last error", last_error.strip()[-3000:]]
    parts += ["", "Continue with the next unticked step in PLAN.md."]
    return "\n".join(parts)


def _alive_in_group(pgid: int) -> list[int]:
    """Live (non-zombie) processes in a process group, read from /proc."""
    alive = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            stat = open(f"/proc/{d}/stat").read()
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[1].split()
        if fields[0] != "Z" and int(fields[2]) == pgid:
            alive.append(int(d))
    return alive


def kill_process_group(pgid: int, grace_s: float = 10.0, proc=None) -> bool:
    """SIGTERM the whole group, then SIGKILL anything left. True if gone.
    Pass the Popen as proc so its zombie gets reaped."""
    def gone():
        if proc is not None:
            proc.poll()
        return not _alive_in_group(pgid)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if gone():
            return True
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    time.sleep(0.3)
    return gone()


def model_server_ok(url: str = "http://127.0.0.1:8080/v1/models", timeout: float = 5.0) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False
