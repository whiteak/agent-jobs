"""Upgrade 9b: ntfy push notifications (titles/status/counts only)."""
from __future__ import annotations

import json
import re
import secrets
import string
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

EVENTS = {"job_finished", "needs_approval", "safety_stop", "bench_finished", "morning_report", "model_down"}


@dataclass
class NotifySettings:
    server: str = ""                     # empty = off
    topic: str = field(default_factory=lambda: random_topic())
    token: str = ""
    events: set = field(default_factory=lambda: set(EVENTS))
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"
    base_url: str = ""                   # set when phone access is on


def random_topic(n: int = 20) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "aj-" + "".join(secrets.choice(alphabet) for _ in range(n))


def validate(s: NotifySettings) -> list[str]:
    errs = []
    if s.server and not re.match(r"^https?://[^\s/]+", s.server):
        errs.append("Server must start with http:// or https://")
    if not re.match(r"^[A-Za-z0-9_-]{8,64}$", s.topic or ""):
        errs.append("Topic must be 8–64 letters, digits, - or _")
    for t in (s.quiet_start, s.quiet_end):
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t or ""):
            errs.append(f"Time {t!r} must look like 22:00")
    return errs


def in_quiet_hours(s: NotifySettings, now: datetime) -> bool:
    cur = now.hour * 60 + now.minute
    a = int(s.quiet_start[:2]) * 60 + int(s.quiet_start[3:])
    b = int(s.quiet_end[:2]) * 60 + int(s.quiet_end[3:])
    return (a <= cur < b) if a < b else (cur >= a or cur < b)


_SAFE_TEXT = re.compile(r"[^\w\s.,:;!?()/%+#✓✗–-]")


def clean(text: str, limit: int = 120) -> str:
    """Strip anything that looks like code, paths or log output."""
    text = re.sub(r"`[^`]*`", "", text)
    text = re.sub(r"(/[\w.-]+){2,}", "", text)          # file paths
    text = _SAFE_TEXT.sub("", text.splitlines()[0] if text else "")
    return " ".join(text.split())[:limit]


def build(event: str, title: str, lines: list[str], job_id: str = "", s: Optional[NotifySettings] = None) -> dict:
    msg = {"title": clean(title, 80), "message": "\n".join(clean(x) for x in lines[:2]) or " ",
           "priority": 5 if event == "safety_stop" else (4 if event == "needs_approval" else 3),
           "tags": [{"safety_stop": "no_entry", "job_finished": "robot", "needs_approval": "eyes",
                     "bench_finished": "bar_chart", "morning_report": "sunrise", "model_down": "warning"}.get(event, "bell")]}
    if s and s.base_url and job_id:
        msg["click"] = f"{s.base_url.rstrip('/')}/jobs/{job_id}"
    return msg


def send(s: NotifySettings, event: str, msg: dict, *, now: Optional[datetime] = None,
         post: Optional[Callable] = None, retries: int = 3, sleep=time.sleep, log=print) -> bool:
    if not s.server or event not in s.events:
        return False
    if event != "safety_stop" and in_quiet_hours(s, now or datetime.now()):
        return False
    body = dict(msg, topic=s.topic)
    post = post or _post
    for i in range(retries):
        try:
            post(s.server.rstrip("/"), body, s.token)
            return True
        except Exception as e:                       # never block a job on this
            log(f"notify: send failed ({e}), try {i + 1}/{retries}")
            sleep(2 ** i)
    return False


def _post(server: str, body: dict, token: str) -> None:
    req = urllib.request.Request(server, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status >= 300:
            raise RuntimeError(f"HTTP {r.status}")
