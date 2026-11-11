"""Upgrade 2: git checkpoints and automatic rollback.

* Snapshots never touch the agent's files, index or branch: they're built
  with a temporary index (GIT_INDEX_FILE) + write-tree + commit-tree.
* Snapshots are tested in a separate detached worktree.
* Rollback only happens at gate points (end of the agent run / a fix round),
  never while the agent is mid-edit.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

from .safegit import GitError, SafeGit
from . import checks as ck

TICK_RE = re.compile(r"^\s*[-*]\s*\[[xX]\]\s*(.+)$", re.M)
COPY_IGNORED = ["local.properties", "node_modules", ".env.test"]


@dataclass
class Checkpoint:
    n: int
    step: str
    commit: str
    passed: int
    failed: int
    build_ok: bool
    time: float

    def score(self) -> tuple:
        return (self.build_ok, self.passed, -self.failed)


def ticked_steps(plan_text: str) -> list[str]:
    return [m.strip() for m in TICK_RE.findall(plan_text or "")]


class CheckpointManager:
    def __init__(self, job_id: str, project: Path, state_dir: Path, *,
                 run_checks_in: Callable[[Path], dict],
                 git: Optional[SafeGit] = None, max_rollbacks: int = 2,
                 emit: Callable[[str, str, dict], None] = lambda *a: None):
        """run_checks_in(folder) must run the (fast) checks in that folder,
        inside the jail, and return a checks.summarize() dict."""
        self.job_id = re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)
        self.project = Path(project)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.git = git or SafeGit(self.project)
        self.run_checks_in = run_checks_in
        self.max_rollbacks = max_rollbacks
        self.emit = emit
        self.checkpoints: list[Checkpoint] = []
        self.rollbacks = 0
        self.discarded: list[str] = []
        self._seen_ticks = 0
        self._busy = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.base = self.git.head()
        self._load()

    # ------------------------------------------------------------ persistence
    @property
    def _record(self) -> Path:
        return self.state_dir / "checkpoints.json"

    def _save(self) -> None:
        data = {"checkpoints": [asdict(c) for c in self.checkpoints], "rollbacks": self.rollbacks,
                "discarded": self.discarded, "seen_ticks": self._seen_ticks, "base": self.base}
        tmp = self._record.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self._record)

    def _load(self) -> None:
        if self._record.is_file():
            d = json.loads(self._record.read_text())
            self.checkpoints = [Checkpoint(**c) for c in d.get("checkpoints", [])]
            self.rollbacks = d.get("rollbacks", 0)
            self.discarded = d.get("discarded", [])
            self._seen_ticks = d.get("seen_ticks", 0)
            self.base = d.get("base", self.base)

    def to_record(self) -> dict:
        return {"checkpoints": [asdict(c) for c in self.checkpoints], "rollbacks": self.rollbacks,
                "discarded": self.discarded}

    @property
    def rollbacks_exhausted(self) -> bool:
        return self.rollbacks > self.max_rollbacks

    # ------------------------------------------------------------ snapshots
    def snapshot(self, message: str) -> str:
        """Commit object of the current work tree (incl. untracked, not ignored
        files) without touching the real index, work tree or branch."""
        idx = self.project / ".git" / f"agent-snap-index-{self.job_id}"
        env = {"GIT_INDEX_FILE": str(idx)}
        try:
            head = self.git.head()
            if head:
                self.git.run("read-tree", head, env=env)
            else:
                idx.unlink(missing_ok=True)
            self.git.run("add", "-A", ".", env=env)
            tree = self.git.run("write-tree", env=env).strip()
            args = ["commit-tree", tree, "-m", message]
            if head:
                args[2:2] = ["-p", head]
            return self.git.run(*args).strip()
        finally:
            idx.unlink(missing_ok=True)

    def _worktree_for(self, commit: str, n: int) -> Path:
        wt = self.state_dir / f"wt-{n}"
        if wt.exists():
            self._remove_worktree(wt)
        self.git.run("worktree", "add", "--detach", str(wt), commit)
        for name in COPY_IGNORED:               # ignored files a build may need
            src = self.project / name
            if src.is_dir():
                (wt / name).symlink_to(src, target_is_directory=True)
            elif src.is_file():
                shutil.copy2(src, wt / name)
        return wt

    def _remove_worktree(self, wt: Path) -> None:
        self.git.ok("worktree", "remove", "--force", str(wt))
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
        self.git.ok("worktree", "prune")

    def test_snapshot(self, step: str) -> Optional[Checkpoint]:
        """Snapshot + test in a worktree. Records a checkpoint if checks pass."""
        n = len(self.checkpoints) + 1
        commit = self.snapshot(f"checkpoint: step {n}: {step}"[:200])
        wt = self._worktree_for(commit, n)
        try:
            summary = self.run_checks_in(wt)
        finally:
            self._remove_worktree(wt)
        passed, failed, build_ok = ck.counts_total(summary)
        if summary.get("status") != "passed":
            self.emit("checkpoint_skipped", f"Step {n} snapshot didn't pass its checks", {"step": step})
            return None
        return self._record_checkpoint(n, step, commit, passed, failed, build_ok)

    def _record_checkpoint(self, n, step, commit, passed, failed, build_ok) -> Checkpoint:
        self.git.run("update-ref", f"refs/agent/{self.job_id}/ckpt-{n}", commit)
        cp = Checkpoint(n, step, commit, passed, failed, build_ok, time.time())
        self.checkpoints.append(cp)
        self._save()
        self.emit("checkpoint", f"Checkpoint after step {n}: {passed} tests passing",
                  {"n": n, "commit": commit})
        return cp

    def on_plan_change(self, plan_text: str, background: bool = True) -> bool:
        """Call whenever PLAN.md changes. Starts a snapshot test when a new step
        was ticked; skips if the previous one is still running."""
        ticks = ticked_steps(plan_text)
        if len(ticks) <= self._seen_ticks:
            return False
        if not self._busy.acquire(blocking=False):
            return False
        step = ticks[-1]
        self._seen_ticks = len(ticks)

        def work():
            try:
                self.test_snapshot(step)
            except (GitError, OSError) as e:
                self.emit("checkpoint_error", f"Checkpoint failed: {e}", {})
            finally:
                self._busy.release()

        if background:
            self._thread = threading.Thread(target=work, daemon=True)
            self._thread.start()
        else:
            work()
        return True

    def wait_idle(self, timeout: float = 3600) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------------ gate logic
    def best(self) -> Optional[Checkpoint]:
        if not self.checkpoints:
            return None
        return max(reversed(self.checkpoints), key=lambda c: c.score())

    def at_gate(self, summary: dict) -> Optional[str]:
        """Compare the current result with the best checkpoint. If it's worse,
        save the current state on a discarded branch, reset to the best
        checkpoint and return a message for the next fix round."""
        self.wait_idle()
        passed, failed, build_ok = ck.counts_total(summary)
        best = self.best()
        if summary.get("status") == "passed" and (best is None or (build_ok, passed) >= (best.build_ok, best.passed)):
            self._record_checkpoint(len(self.checkpoints) + 1, "checks passed at gate",
                                    self.snapshot("checkpoint: checks passed"), passed, failed, build_ok)
            return None
        if best is None:
            return None
        worse = []
        if best.build_ok and not build_ok:
            worse.append("the build stopped working")
        if passed < best.passed:
            worse.append(f"passing tests dropped from {best.passed} to {passed}")
        if not worse:
            return None
        self.rollbacks += 1
        k = len(self.discarded) + 1
        branch = f"agent/{self.job_id}-discarded-{k}"
        bad = self.snapshot(f"discarded attempt {k}")
        self.git.run("update-ref", f"refs/heads/{branch}", bad)
        self.discarded.append(branch)
        self._reset_to(best.commit)
        self._save()
        what = " and ".join(worse)
        return (f"Your last changes made things worse ({what}). They were rolled back to the "
                f"checkpoint after step {best.n} ({best.step}). Your attempt is saved on branch "
                f"{branch}. Try a different approach.")

    def _reset_to(self, commit: str) -> None:
        self.git.run("reset", "--hard", commit)
        self.git.run("clean", "-fd")

    def rollback_to(self, n: int) -> Checkpoint:
        """Manual 'Roll back to here' (only when the job isn't running)."""
        cp = next(c for c in self.checkpoints if c.n == n)
        k = len(self.discarded) + 1
        branch = f"agent/{self.job_id}-discarded-{k}"
        self.git.run("update-ref", f"refs/heads/{branch}", self.snapshot(f"before manual rollback to {n}"))
        self.discarded.append(branch)
        self._reset_to(cp.commit)
        self._save()
        self.emit("rollback", f"Rolled back to checkpoint {n} by hand", {"n": n})
        return cp

    def diff_from_previous(self, n: int) -> str:
        cps = {c.n: c for c in self.checkpoints}
        prev = cps[n - 1].commit if n - 1 in cps else (self.base or "")
        if not prev:
            return self.git.run("show", "--no-ext-diff", "--no-textconv", "--format=", cps[n].commit)
        return self.git.run("diff", "--no-ext-diff", "--no-textconv", prev, cps[n].commit)

    # ------------------------------------------------------------ clean-up
    def finish(self) -> None:
        self.wait_idle()
        for wt in self.state_dir.glob("wt-*"):
            self._remove_worktree(wt)


def cleanup_old(git: SafeGit, days: int = 14, now: Optional[float] = None) -> list[str]:
    """Delete checkpoint refs and discarded branches older than `days`."""
    now = now or time.time()
    out = git.run("for-each-ref", "--format=%(refname) %(committerdate:unix)",
                  "refs/agent/", "refs/heads/agent/")
    removed = []
    for line in out.splitlines():
        ref, _, ts = line.rpartition(" ")
        if not ref:
            continue
        if ref.startswith("refs/heads/") and "-discarded-" not in ref:
            continue
        if now - int(ts) > days * 86400:
            git.run("update-ref", "-d", ref)
            removed.append(ref)
    return removed
