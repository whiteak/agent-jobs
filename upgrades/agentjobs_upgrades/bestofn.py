"""Upgrade 5: best-of-N attempt selection."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Attempt:
    k: int
    branch: str
    status: str                 # passed | failed | needs_review | stuck | safety_stop | unverified
    tests_passed: int = 0
    test_files_changed: int = 0
    diff_lines: int = 0
    seconds: float = 0.0
    issues: list = field(default_factory=list)

    @property
    def clean_pass(self) -> bool:
        return self.status == "passed" and not self.issues


def sort_key(a: Attempt) -> tuple:
    return (0 if a.clean_pass else 1, -a.tests_passed, a.test_files_changed, a.diff_lines, a.seconds, a.k)


def pick_winner(attempts: list[Attempt]) -> tuple[Attempt | None, list[Attempt]]:
    """Returns (winner or None if none passed cleanly, attempts ranked best first)."""
    usable = [a for a in attempts if a.status != "safety_stop"]
    ranked = sorted(usable, key=sort_key) + [a for a in attempts if a.status == "safety_stop"]
    winner = ranked[0] if ranked and ranked[0].clean_pass else None
    return winner, ranked


def should_continue(done: list[Attempt], n: int, hard: bool, stop_early: bool = True) -> bool:
    if len(done) >= n:
        return False
    if any(a.status == "safety_stop" for a in done):
        return False
    if stop_early and not hard and done and done[0].clean_pass:
        return False
    return True


def why(winner: Attempt, ranked: list[Attempt]) -> str:
    if winner is None:
        return "No attempt passed its checks."
    others = [a for a in ranked if a is not winner]
    if not others:
        return f"Attempt {winner.k} passed."
    r = others[0]
    if not r.clean_pass:
        return f"Attempt {winner.k} was the only one that passed cleanly."
    for label, a, b in (("more tests passing", winner.tests_passed, r.tests_passed),
                        ("fewer test files changed", -winner.test_files_changed, -r.test_files_changed),
                        ("a smaller change", -winner.diff_lines, -r.diff_lines),
                        ("finished faster", -winner.seconds, -r.seconds)):
        if a != b:
            return f"Attempt {winner.k} won: {label}."
    return f"Attempt {winner.k} won (tie)."
