"""Upgrade 7a: job events (timeline) stored as JSON lines + Server-Sent Events."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

ICONS = {"queued": "🕒", "started": "▶", "step": "☑", "checks": "🧪", "fix_round": "🔧",
         "checkpoint": "📌", "rollback": "↩", "watchdog": "🐕", "nudge": "🐕", "restart": "🔄",
         "safety_stop": "⛔", "needs_review": "⚠", "finished": "🏁", "failed": "✗",
         "approved": "✅", "merged": "🔀", "rejected": "🗑", "pause": "⏸", "resume": "▶",
         "attempt": "🎲", "plan": "🗒", "queue": "🚂"}   # U07: one icon per timeline row

_lock = threading.Lock()


class JobEvents:
    def __init__(self, path: Path):
        self.path = Path(path)

    def add(self, type: str, text: str, data: Optional[dict] = None) -> dict:
        with _lock:
            n = self._count()
            ev = {"id": n + 1, "time": time.time(), "type": type, "text": text, "data": data or {}}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            return ev

    def _count(self) -> int:
        if not self.path.exists():
            return 0
        with open(self.path, "rb") as f:
            return sum(1 for _ in f)

    def list(self, after: int = 0) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("id", 0) > after:
                out.append(ev)
        return out

    def emitter(self):
        """Callable(kind, text, data) to hand to VerifyGate / CheckpointManager."""
        return lambda kind, text, data=None: self.add(kind, text, data)


def sse_format(ev: dict, event: str = "job") -> str:
    data = json.dumps(ev, ensure_ascii=False)
    return f"id: {ev['id']}\nevent: {event}\ndata: {data}\n\n"


def sse_stream(events: JobEvents, last_id: int = 0, poll_s: float = 1.0,
               keepalive_s: float = 15.0, stop=lambda: False) -> Iterator[str]:
    """Yields SSE chunks forever (until stop()). Honours Last-Event-ID via last_id."""
    yield "retry: 3000\n\n"
    last_ping = time.time()
    while not stop():
        new = events.list(after=last_id)
        for ev in new:
            last_id = ev["id"]
            yield sse_format(ev)
        if time.time() - last_ping >= keepalive_s:
            last_ping = time.time()
            yield ": keepalive\n\n"
        if not new:
            time.sleep(poll_s)


BADGES = {
    "running": ("Working", "blue"), "queued": ("Queued", "grey"), "passed": ("Done ✓", "green"),
    "failed": ("Failed ✗", "red"), "unverified": ("Not verified", "yellow"),
    "needs_review": ("Needs review", "orange"), "safety_stop": ("Safety stop", "red"),
    "stuck": ("Stuck", "orange"), "stopped": ("Stopped", "grey"), "interrupted": ("Interrupted", "grey"),
}


def badge(status: str) -> tuple[str, str]:
    return BADGES.get(status, (status.replace("_", " ").capitalize(), "grey"))
