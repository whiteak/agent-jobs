"""Fake agent for tests and demos (upgrade 1a). No model needed.

Usage: python3 -m agentjobs_upgrades.fakeagent SCENARIO.json [--round N] [--log FILE]
                                            [--format simple|stream-json] [--resume ID]

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
--format stream-json writes the same events the way Claude Code's --output-format
stream-json does (system/init, assistant tool_use/text with usage, user tool_result,
result), so the job board's log parser and watchdog see a realistic stream. The
final result line puts "type" LAST, like current Claude Code does.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
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
    ap.add_argument("--format", choices=("simple", "stream-json"), default="simple")
    ap.add_argument("--resume")
    a = ap.parse_args(argv)
    sc = json.loads(Path(a.scenario).read_text())
    steps = sc
    if isinstance(sc, dict):
        rounds = sc.get("rounds", [])
        steps = rounds[min(a.round, len(rounds) - 1)] if rounds else []
    out = open(a.log, "a") if a.log else sys.stdout

    def log(**kw):
        kw["time"] = time.time()
        if a.format == "stream-json":
            for ev in _as_stream(kw, state):
                out.write(json.dumps(ev) + "\n")
        else:
            out.write(json.dumps(kw) + "\n")
        out.flush()

    state = {"session": a.resume or "fake-" + secrets.token_hex(8), "n": 0, "said": "", "turns": 0}

    log(type="start", round=a.round)
    for st in steps:
        if "write" in st:
            p = Path(st["write"]); p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(st.get("content", "")); log(type="write", path=st["write"], content=st.get("content", ""))
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


def _as_stream(kw, state):
    """One simple event -> the stream-json events Claude Code would print."""
    sid = state["session"]
    t = kw["type"]

    def tool(name, inp):
        state["n"] += 1
        state["turns"] += 1
        tid = f"toolu_fake{state['n']:04d}"
        state["last_tool"] = tid
        return tid, {"type": "assistant", "session_id": sid, "message": {
            "role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
            "usage": {"input_tokens": 100, "output_tokens": 10}}}

    if t == "start":
        return [{"type": "system", "subtype": "init", "session_id": sid, "cwd": os.getcwd(),
                 "model": "fake-agent", "tools": ["Bash", "Write", "Edit"]}]
    if t == "write":
        p = os.path.abspath(kw["path"])
        return [tool("Write", {"file_path": p, "content": kw.get("content", "")})[1],
                {"type": "user", "session_id": sid, "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": state["last_tool"], "content": "ok", "is_error": False}]}}]
    if t == "delete":
        _, ev = tool("Bash", {"command": f"rm -f {kw['path']}"})
        return [ev]
    if t == "command":
        _, ev = tool("Bash", {"command": kw["cmd"]})
        return [ev]
    if t == "result":
        return [{"type": "user", "session_id": sid, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": state.get("last_tool", ""), "content": kw.get("output", ""),
             "is_error": kw.get("code", 0) != 0}]}}]
    if t == "message":
        state["said"] = kw["text"]
        return [{"type": "assistant", "session_id": sid, "message": {
            "role": "assistant", "content": [{"type": "text", "text": kw["text"]}],
            "usage": {"input_tokens": 100, "output_tokens": 10}}}]
    if t == "usage":
        return [{"type": "assistant", "session_id": sid, "message": {
            "role": "assistant", "content": [{"type": "text", "text": "(working)"}],
            "usage": {"input_tokens": kw["tokens"], "output_tokens": 1}}}]
    if t == "exit":
        code = kw.get("code", 0)
        return [{"duration_ms": 1, "num_turns": state["turns"], "result": state["said"] or "Done.",
                 "session_id": sid, "is_error": code != 0, "subtype": "success" if code == 0 else "error",
                 "type": "result"}]
    return []


if __name__ == "__main__":
    sys.exit(main())
