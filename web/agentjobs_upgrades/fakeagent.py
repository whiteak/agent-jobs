"""Fake agent for tests and demos (upgrade 1a). No model needed.

Usage: python3 -m agentjobs_upgrades.fakeagent SCENARIO.json [--round N] [--log FILE]

Scenario: {"rounds": [[step, ...], [step, ...]]} or a plain list of steps
(used for every round). Steps:
  {"write": "path", "content": "..."}     create/overwrite a file
  {"append": "path", "content": "..."}
  {"delete": "path"}
  {"tick": 2}                              tick the first N unticked PLAN.md boxes
  {"cmd": "shell command", "run": true}   log a command (and run it if run=true)
  {"say": "text"}                          log an assistant message
  {"tokens": 12345}                        log token usage
  {"sleep": 1.5}
  {"exit": 0}
Log lines are JSON objects, one per line, written to --log (default stdout).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path


def tick_plan(n: int, plan: Path = Path("PLAN.md")) -> None:
    text = plan.read_text() if plan.exists() else ""
    for _ in range(n):
        text, k = re.subn(r"^(\s*[-*]\s*)\[ \]", r"\1[x]", text, count=1, flags=re.M)
        if not k:
            break
    plan.write_text(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--round", type=int, default=0)
    ap.add_argument("--log")
    a = ap.parse_args(argv)
    sc = json.loads(Path(a.scenario).read_text())
    steps = sc
    if isinstance(sc, dict):
        rounds = sc.get("rounds", [])
        steps = rounds[min(a.round, len(rounds) - 1)] if rounds else []
    out = open(a.log, "a") if a.log else sys.stdout

    def log(**kw):
        kw["time"] = time.time()
        out.write(json.dumps(kw) + "\n")
        out.flush()

    log(type="start", round=a.round)
    for st in steps:
        if "write" in st:
            p = Path(st["write"]); p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(st.get("content", "")); log(type="write", path=st["write"])
        elif "append" in st:
            with open(st["append"], "a") as f:
                f.write(st.get("content", ""))
            log(type="write", path=st["append"])
        elif "delete" in st:
            Path(st["delete"]).unlink(missing_ok=True); log(type="delete", path=st["delete"])
        elif "tick" in st:
            tick_plan(int(st["tick"])); log(type="write", path="PLAN.md")
        elif "cmd" in st:
            log(type="command", cmd=st["cmd"])
            if st.get("run"):
                p = subprocess.run(st["cmd"], shell=True, capture_output=True, text=True)
                log(type="result", cmd=st["cmd"], code=p.returncode, output=(p.stdout + p.stderr)[-4000:])
        elif "say" in st:
            log(type="message", text=st["say"])
        elif "tokens" in st:
            log(type="usage", tokens=int(st["tokens"]))
        elif "sleep" in st:
            time.sleep(float(st["sleep"]))
        elif "exit" in st:
            log(type="exit", code=int(st["exit"]))
            return int(st["exit"])
    log(type="exit", code=0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
