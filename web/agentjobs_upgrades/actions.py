"""Upgrade 8: approve / merge / reject, protected by a human-only secret.

The secret file lives outside every folder the agent jail can read (default
~/.local/state/agentjobs/secret, mode 600). The page gets it as an HttpOnly
cookie plus a hidden form field; both must match on every POST.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from .safegit import GitError, SafeGit

DEFAULT_SECRET = Path.home() / ".local/state/agentjobs/secret"
MERGEABLE = {"passed"}


def load_secret(path: Path = DEFAULT_SECRET) -> str:
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    return path.read_text().strip()


def form_token(secret: str, session_id: str) -> str:
    return hmac.new(secret.encode(), b"form:" + session_id.encode(), "sha256").hexdigest()


def check_request(secret: str, method: str, cookie_session: Optional[str], form_tok: Optional[str]) -> bool:
    """True only for a POST that carries the session cookie and its matching form token."""
    if method.upper() != "POST" or not cookie_session or not form_tok:
        return False
    return hmac.compare_digest(form_token(secret, cookie_session), form_tok)


def merge_allowed(status: str, typed: str = "") -> tuple[bool, str]:
    if status in MERGEABLE:
        return True, ""
    if typed.strip() == "MERGE":
        return True, "merged anyway"
    return False, "Checks didn't pass. Type MERGE to merge anyway."


class Decisions:
    """Merge / reject / undo on a project's own work copy. Local only: never pushes."""

    def __init__(self, repo: Path, git: Optional[SafeGit] = None):
        self.repo = Path(repo)
        self.git = git or SafeGit(self.repo)

    def _clean(self) -> bool:
        return self.git.run("status", "--porcelain").strip() == ""

    def merge(self, job_id: str, job_branch: str, target: str, title: str) -> dict:
        if not self._clean():
            return {"ok": False, "reason": "The project has uncommitted changes. Commit or clear them first."}
        before_branch = self.git.branch()
        target_before = self.git.run("rev-parse", target).strip()
        try:
            self.git.run("checkout", "-q", target)
            if self.git.ok("merge-base", "--is-ancestor", target, job_branch):
                self.git.run("merge", "--ff-only", "-q", job_branch)
                how = "fast-forward"
            else:
                try:
                    self.git.run("merge", "--no-ff", "-q", "-m", f"Merge agent job {job_id}: {title}", job_branch)
                    how = "merge commit"
                except GitError as e:
                    self.git.ok("merge", "--abort")
                    self.git.run("reset", "-q", "--hard", target_before)
                    return {"ok": False, "conflict": True,
                            "reason": "These changes clash with the target branch. Nothing was changed.",
                            "detail": str(e)[:500]}
            after = self.git.run("rev-parse", target).strip()
            return {"ok": True, "how": how, "target": target, "before": target_before, "after": after,
                    "pushed": False, "time": time.time()}
        finally:
            if before_branch not in ("HEAD", target):
                self.git.ok("checkout", "-q", before_branch)

    def undo_merge(self, rec: dict) -> dict:
        cur = self.git.run("rev-parse", rec["target"]).strip()
        if cur != rec["after"]:
            return {"ok": False, "reason": "Something was committed on top since; undo it by hand."}
        if self.git.branch() == rec["target"]:
            if not self._clean():
                return {"ok": False, "reason": "Uncommitted changes in the project."}
            self.git.run("reset", "-q", "--hard", rec["before"])
        else:
            self.git.run("update-ref", f"refs/heads/{rec['target']}", rec["before"], rec["after"])
        return {"ok": True}

    def reject(self, job_id: str, job_branch: str) -> dict:
        new = f"agent/rejected/{job_id}"
        self.git.run("branch", "-m", job_branch, new)
        return {"ok": True, "branch": new, "old_branch": job_branch, "time": time.time()}

    def unreject(self, rec: dict, days: int = 14) -> dict:
        if time.time() - rec["time"] > days * 86400:
            return {"ok": False, "reason": f"Older than {days} days."}
        self.git.run("branch", "-m", rec["branch"], rec["old_branch"])
        return {"ok": True}


def change_request_prompt(original_prompt: str, request: str) -> str:
    rows = [r for r in original_prompt.strip().splitlines() if r.strip()][:15]
    return ("Follow-up on an earlier job, same branch.\n\nOriginal task (summary):\n" + "\n".join(rows) +
            "\n\nJason asked for these changes:\n" + request.strip() +
            "\n\nMake the changes, keep all tests passing, and update PLAN.md and AGENT_REPORT.md.")
