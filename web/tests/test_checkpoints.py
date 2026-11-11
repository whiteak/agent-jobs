import os, subprocess, tempfile, unittest
from pathlib import Path
from agentjobs_upgrades import checks as ck
from agentjobs_upgrades.checkpoints import CheckpointManager, cleanup_old
from agentjobs_upgrades.safegit import SafeGit
from agentjobs_upgrades.verify import VerifyGate
from tests.helpers import make_py_project, fake_agent_runner, git

GOOD = "def add(a, b):\n    return int(a + b)\n"
BAD = "def add(a, b):\n    return a - b - 0\n"


def status_fingerprint(p):
    st = git(p, "status", "--porcelain")
    idx = (p / ".git/index").read_bytes()
    mt = {f: os.stat(p / f).st_mtime_ns for f in ("calc.py", "PLAN.md")}
    return st, idx, mt, git(p, "rev-parse", "HEAD")


class CP(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.p = make_py_project(self.t)
        self.ev = []
        self.m = CheckpointManager("job1", self.p, self.t / "state",
                                   run_checks_in=lambda d: ck.run_all(d, self.t / "out-wt"),
                                   emit=lambda k, txt, d: self.ev.append(k))

    def test_snapshot_does_not_touch_work_tree(self):
        (self.p / "new.txt").write_text("untracked")
        (self.p / "calc.py").write_text(GOOD)
        before = status_fingerprint(self.p)
        c = self.m.snapshot("x")
        self.assertEqual(before, status_fingerprint(self.p))
        self.assertIn("new.txt", git(self.p, "show", "--name-only", "--format=", c))

    def test_checkpoint_on_tick_and_rollback(self):
        (self.p / "PLAN.md").write_text("- [x] step one\n- [ ] two\n")
        self.assertTrue(self.m.on_plan_change((self.p / "PLAN.md").read_text(), background=False))
        self.assertEqual(len(self.m.checkpoints), 1)
        self.assertEqual(self.m.checkpoints[0].passed, 2)
        self.assertFalse(self.m.on_plan_change((self.p / "PLAN.md").read_text(), background=False))
        self.assertEqual(git(self.p, "rev-parse", "refs/agent/job1/ckpt-1").strip(), self.m.checkpoints[0].commit)
        # things get worse
        (self.p / "calc.py").write_text(BAD)
        s = ck.run_all(self.p, self.t / "o")
        msg = self.m.at_gate(s)
        self.assertIn("passing tests dropped from 2 to 1", msg)
        self.assertEqual((self.p / "calc.py").read_text(), "def add(a, b):\n    return a + b\n")
        self.assertIn("agent/job1-discarded-1", git(self.p, "branch", "--list", "agent/*"))
        self.assertIn("a - b - 0", git(self.p, "show", "agent/job1-discarded-1:calc.py"))

    def test_failing_snapshot_not_recorded(self):
        (self.p / "calc.py").write_text(BAD)
        self.m.on_plan_change("- [x] a\n", background=False)
        self.assertEqual(self.m.checkpoints, [])

    def test_rollback_limit_in_gate(self):
        self.m.on_plan_change("- [x] a\n", background=False)
        (self.p / "calc.py").write_text(BAD)
        g = VerifyGate(self.p, fake_agent_runner(self.p, [{"write": "calc.py", "content": BAD}], self.t / "l"),
                       lambda: ck.run_all(self.p, self.t / "o"), checkpoints=self.m, max_rounds=5)
        o = g.run("x")
        self.assertEqual(o["status"], "stuck")
        self.assertEqual(self.m.rollbacks, 3)

    def test_gate_recovers_after_rollback(self):
        self.m.on_plan_change("- [x] a\n", background=False)
        (self.p / "calc.py").write_text(BAD)
        g = VerifyGate(self.p, fake_agent_runner(self.p, {"rounds": [[], [], [{"write": "calc.py", "content": GOOD}]]}, self.t / "l"),
                       lambda: ck.run_all(self.p, self.t / "o"), checkpoints=self.m)
        o = g.run("x")
        # round 1 starts from the rolled-back (good) state, so checks pass right away
        self.assertEqual(o["status"], "passed")
        self.assertIn("rolled back", (self.t / "prompt-1.txt").read_text())

    def test_manual_rollback_and_diff(self):
        (self.p / "calc.py").write_text(GOOD)
        self.m.on_plan_change("- [x] a\n", background=False)
        (self.p / "x.py").write_text("print(1)\n")
        self.m.on_plan_change("- [x] a\n- [x] b\n", background=False)
        self.assertIn("x.py", self.m.diff_from_previous(2))
        self.m.rollback_to(1)
        self.assertFalse((self.p / "x.py").exists())

    def test_persist_and_cleanup(self):
        self.m.on_plan_change("- [x] a\n", background=False)
        m2 = CheckpointManager("job1", self.p, self.t / "state", run_checks_in=lambda d: {})
        self.assertEqual(len(m2.checkpoints), 1)
        self.assertEqual(cleanup_old(SafeGit(self.p), days=14), [])
        removed = cleanup_old(SafeGit(self.p), days=14, now=2_000_000_000 + 10**9)
        self.assertIn("refs/agent/job1/ckpt-1", removed)

    def test_planted_hook_not_run(self):
        hook = self.p / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\ntouch " + str(self.t / "PWNED") + "\n"); hook.chmod(0o755)
        (self.p / ".git/hooks/pre-commit").write_text(hook.read_text()); (self.p / ".git/hooks/pre-commit").chmod(0o755)
        self.m.on_plan_change("- [x] a\n", background=False)
        (self.p / "calc.py").write_text(BAD)
        self.m.at_gate(ck.run_all(self.p, self.t / "o"))
        self.assertFalse((self.t / "PWNED").exists())


if __name__ == "__main__":
    unittest.main()
