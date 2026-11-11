"""Git helper that doesn't trust the job repo.

The agent controls the repo, so it could plant hooks, an fsmonitor command,
filter drivers or a pager in .git/config. Every git call made by the tool on
a job repo goes through here: hooks and fsmonitor are switched off on the
command line, and the call can be wrapped in the agent's jail so whatever
config is left only ever runs with the agent's own (no-network) rights.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Callable, Optional

SAFE_FLAGS = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
              "-c", "core.pager=cat", "-c", "core.sshCommand=false",
              "-c", "protocol.allow=never", "-c", "credential.helper=",
              "-c", "core.editor=true", "-c", "commit.gpgSign=false",
              "-c", "tag.gpgSign=false", "-c", "user.name=Agent Jobs",
              "-c", "user.email=agent-jobs@localhost"]

Wrap = Callable[[Path, str], list]


class GitError(RuntimeError):
    pass


class SafeGit:
    def __init__(self, repo: Path, wrap: Optional[Wrap] = None, timeout: int = 300):
        self.repo = Path(repo)
        self.wrap = wrap
        self.timeout = timeout

    def run(self, *args: str, env: Optional[dict] = None, check: bool = True,
            input: Optional[str] = None) -> str:
        argv = ["git", *SAFE_FLAGS, *args]
        env_all = dict(os.environ)
        env_all.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
                        "GIT_CONFIG_NOSYSTEM": "1"})
        if env:
            env_all.update(env)
        if self.wrap is not None:
            # env vars may not cross the jail, so put the important ones on the command line
            prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
            cmd = (f"env {prefix} " if prefix else "") + shlex.join(argv)
            argv = self.wrap(self.repo, cmd)
        p = subprocess.run(argv, cwd=str(self.repo), env=env_all, capture_output=True,
                           text=True, timeout=self.timeout, input=input)
        if check and p.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed ({p.returncode}): {p.stderr.strip()[:500]}")
        return p.stdout

    def ok(self, *args: str) -> bool:
        try:
            self.run(*args)
            return True
        except (GitError, subprocess.TimeoutExpired):
            return False

    def head(self) -> Optional[str]:
        try:
            return self.run("rev-parse", "--verify", "-q", "HEAD").strip() or None
        except GitError:
            return None

    def branch(self) -> str:
        return self.run("rev-parse", "--abbrev-ref", "HEAD").strip()
