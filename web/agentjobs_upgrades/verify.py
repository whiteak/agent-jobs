"""Upgrade 1b: the verify-and-retry gate.

A job only counts as done when its checks pass. Failing checks go back to
the agent automatically for up to `max_rounds` fix rounds. The cheating
guard turns a "pass" into "needs review" if tests were weakened.

The runner supplies:
  run_agent(prompt: str, round_no: int) -> None   # blocks until the agent run ends
  run_checks() -> dict                           # checks.run_all(...) summary
and optionally a checkpoint manager (upgrade 2) with
  at_gate(summary) -> Optional[str]   # returns a rollback message, or None
  rollbacks_exhausted -> bool
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from . import checks as ck
from . import guard

MAX_PROMPT_CHARS = 24_000          # ~6,000 tokens of check output in a fix prompt
TAIL_LINES = 120

FIX_RULES = ("Fix the cause so these checks pass. Do not delete, skip or weaken tests, "
             "and do not change the check commands. Update PLAN.md and AGENT_REPORT.md.")

Emit = Callable[[str, str, dict], None]


def _noop_emit(kind: str, text: str, data: dict) -> None:
    pass


def short_task(task: str, lines: int = 2, chars: int = 400) -> str:
    rows = [r.strip() for r in task.strip().splitlines() if r.strip()]
    s = " ".join(rows[:lines])
    return s if len(s) <= chars else s[:chars - 1] + "…"


def build_fix_prompt(task: str, summary: dict, round_no: int, max_rounds: int,
                     rollback_msg: Optional[str] = None) -> str:
    failing = [r for r in summary.get("results", [])
               if not r.get("ok") and r.get("mode", "required") != "advisory"]
    parts = [f"Fix round {round_no} of {max_rounds}.",
             f"Original task: {short_task(task)}", ""]
    if rollback_msg:
        parts += [rollback_msg, ""]
    names = ", ".join(r["name"] for r in failing) or "none"
    parts.append(f"These checks failed: {names}.")
    budget = MAX_PROMPT_CHARS // max(1, len(failing))
    for r in failing:
        out = ""
        try:
            out = Path(r.get("output_file", "")).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        block = [f"\n### {r['name']}: `{r['cmd']}` (exit code {r.get('exit_code')})"]
        if r.get("timed_out"):
            block.append("It took too long and was stopped.")
        if r.get("issues"):                      # vision checks
            block.append("Problems seen on screen:\n- " + "\n- ".join(r["issues"]))
        first = r.get("first_error") or ck.first_error_block(out)
        last = ck.tail(out, TAIL_LINES)
        body = "First error:\n```\n" + first + "\n```\n"
        if last and last.strip() not in first:
            body += f"Last {TAIL_LINES} lines:\n```\n" + last + "\n```"
        if len(body) > budget:
            keep = max(400, budget // 2 - 40)
            body = body[:keep] + "\n…(cut)…\n" + body[-keep:]
        block.append(body)
        parts.append("\n".join(block))
    parts += ["", FIX_RULES]
    return "\n".join(parts)


def _test_count(summary: dict) -> Optional[int]:
    total, seen = 0, False
    for r in summary.get("results", []):
        vals = [r.get("passed"), r.get("failed"), r.get("skipped")]
        if any(v is not None for v in vals):
            seen = True
            total += sum(v or 0 for v in vals)
    return total if seen else None


class VerifyGate:
    def __init__(self, project: Path, run_agent: Callable[[str, int], None],
                 run_checks: Callable[[], dict], *, max_rounds: int = 3,
                 verify: bool = True, emit: Emit = _noop_emit, checkpoints=None):
        self.project = Path(project)
        self.run_agent = run_agent
        self.run_checks = run_checks
        self.max_rounds = max_rounds
        self.verify = verify
        self.emit = emit
        self.checkpoints = checkpoints

    def run(self, task: str) -> dict:
        """Call after the agent's first run has ended. Returns the outcome dict
        stored in the job record: {status, rounds, issues, banner}."""
        outcome = {"status": "unverified", "rounds": [], "issues": [], "started": time.time()}
        if not self.verify:
            self.emit("checks", "Checks skipped (Don't verify was ticked)", {})
            return self._finish(outcome, {"status": "unverified", "results": []})

        summary = self.run_checks()
        outcome["rounds"].append({"round": 0, "summary": summary})
        self._emit_checks(0, summary)
        if summary["status"] in ("passed", "unverified"):
            return self._finish(outcome, summary)

        before = guard.snapshot(self.project, _test_count(summary))
        rollback_msg = None
        for rnd in range(1, self.max_rounds + 1):
            if self.checkpoints is not None:
                rollback_msg = self.checkpoints.at_gate(summary)
                if rollback_msg:
                    self.emit("rollback", rollback_msg, {"round": rnd})
                if getattr(self.checkpoints, "rollbacks_exhausted", False):
                    outcome["status"] = "stuck"
                    return self._finish(outcome, summary, "Stuck – needs a look (rolled back too often)")
            prompt = build_fix_prompt(task, summary, rnd, self.max_rounds, rollback_msg)
            self.emit("fix_round", f"Fix round {rnd} started", {"round": rnd})
            self.run_agent(prompt, rnd)
            summary = self.run_checks()
            after = guard.snapshot(self.project, _test_count(summary))
            issues = guard.compare(before, after)
            outcome["rounds"].append({"round": rnd, "summary": summary, "issues": issues})
            self._emit_checks(rnd, summary)
            if issues:
                outcome["issues"] = issues
                self.emit("needs_review", "Tests were changed in a suspicious way", {"issues": issues})
                summary = dict(summary, status="needs_review", reason=issues[0])
                return self._finish(outcome, summary)
            if summary["status"] == "passed":
                return self._finish(outcome, summary)
        if self.checkpoints is not None:
            msg = self.checkpoints.at_gate(summary)        # end on the best known state
            if msg:
                self.emit("rollback", msg, {"final": True})
        outcome["status"] = "failed"
        return self._finish(outcome, summary,
                            f"Failed after {self.max_rounds} fix rounds")

    # ------------------------------------------------------------------
    def _emit_checks(self, rnd: int, summary: dict) -> None:
        colour, text = ck.banner_text(summary)
        self.emit("checks", text, {"round": rnd, "status": summary["status"], "colour": colour})

    def _finish(self, outcome: dict, summary: dict, text: Optional[str] = None) -> dict:
        if outcome["status"] not in ("failed", "stuck"):
            outcome["status"] = summary["status"]
        colour, banner = ck.banner_text(summary)
        if text:
            colour, banner = ("red", text + ": " + banner.replace("Checks failed: ", "")) \
                if outcome["status"] == "failed" else ("orange", text)
        outcome["banner"] = {"colour": colour, "text": banner}
        outcome["final"] = summary
        outcome["finished"] = time.time()
        outcome["timeline"] = timeline_text(outcome)
        return outcome


def timeline_text(outcome: dict) -> str:
    bits = ["Agent finished"]
    for r in outcome.get("rounds", []):
        if r["round"] > 0:
            bits.append(f"fix round {r['round']}")
        st = r["summary"]["status"]
        if st == "passed":
            bits.append("checks passed")
        elif st == "unverified":
            bits.append("no checks found")
        else:
            n = sum(1 for x in r["summary"].get("results", []) if not x.get("ok"))
            bits.append(f"checks failed ({n})")
        if r.get("issues"):
            bits.append("needs review")
    return " → ".join(bits)
