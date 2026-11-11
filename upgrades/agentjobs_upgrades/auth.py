"""Upgrade 9a: phone login (PBKDF2 password, sessions, lock-out)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

ITER = 310_000
LOCAL = {"127.0.0.1", "::1", "localhost"}


def hash_password(pw: str, salt: Optional[bytes] = None, iterations: int = ITER) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, it, salt, dk = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    test = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
    return hmac.compare_digest(test.hex(), dk)


def is_local(addr: str) -> bool:
    return addr in LOCAL or addr.startswith("127.")


class Lockout:
    def __init__(self, max_tries: int = 5, lock_s: int = 300, now=time.time):
        self.max_tries, self.lock_s, self.now = max_tries, lock_s, now
        self.fails: dict[str, list[float]] = {}
        self.locked_until: dict[str, float] = {}

    def locked(self, who: str) -> bool:
        return self.locked_until.get(who, 0) > self.now()

    def fail(self, who: str) -> None:
        t = self.now()
        lst = [x for x in self.fails.get(who, []) if t - x < self.lock_s] + [t]
        self.fails[who] = lst
        if len(lst) >= self.max_tries:
            self.locked_until[who] = t + self.lock_s
            self.fails[who] = []

    def success(self, who: str) -> None:
        self.fails.pop(who, None)


class Sessions:
    """Session ids stored hashed in a small JSON file."""

    def __init__(self, path: Path, days: int = 30, now=time.time):
        self.path, self.days, self.now = Path(path), days, now

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def create(self) -> str:
        sid = secrets.token_urlsafe(32)
        d = {k: v for k, v in self._load().items() if v > self.now()}
        d[hashlib.sha256(sid.encode()).hexdigest()] = self.now() + self.days * 86400
        self._save(d)
        return sid

    def valid(self, sid: Optional[str]) -> bool:
        if not sid:
            return False
        exp = self._load().get(hashlib.sha256(sid.encode()).hexdigest())
        return bool(exp and exp > self.now())

    def revoke_all(self) -> None:
        self._save({})


def cookie_header(name: str, value: str, days: int = 30) -> str:
    return f"{name}={value}; Max-Age={days * 86400}; Path=/; HttpOnly; SameSite=Strict"


def needs_login(remote_addr: str, sid: Optional[str], sessions: Sessions) -> bool:
    return not is_local(remote_addr) and not sessions.valid(sid)
