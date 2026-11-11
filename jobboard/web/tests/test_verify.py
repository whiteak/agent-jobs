import tempfile, unittest
from pathlib import Path
from agentjobs_upgrades import checks as ck, guard
from agentjobs_upgrades.verify import VerifyGate, build_fix_prompt
from tests.helpers import make_py_project, fake_agent_runner

GOOD = "def add(a, b):\n    return int(a + b)\n"


class Gate(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())

    def gate(self, p, scenario, **kw):
        events = []
        g = VerifyGate(p, fake_agent_runner(p, scenario, self.t / "agent.log"),
                       lambda: ck.run_all(p, self.t / "out"),
                       emit=lambda k, txt, d: events.append((k, txt)), **kw)
        return g, events

    def test_pass_first_time(self):
        p = make_py_project(self.t)
        g, ev = self.gate(p, [])
        o = g.run("add numbers")
        self.assertEqual(o["status"], "passed")
        self.assertEqual(len(o["rounds"]), 1)
        self.assertEqual(o["timeline"], "Agent finished → checks passed")

    def test_fixed_in_round_one(self):
        p = make_py_project(self.t, passing=False)
        g, ev = self.gate(p, {"rounds": [[], [{"write": "calc.py", "content": GOOD}]]})
        o = g.run("Make add() work.\nMore detail")
        self.assertEqual(o["status"], "passed")
        self.assertEqual(o["timeline"], "Agent finished → checks failed (1) → fix round 1 → checks passed")
        prompt = (self.t / "prompt-1.txt").read_text()
        self.assertIn("These checks failed: tests", prompt)
        self.assertIn("Do not delete, skip or weaken tests", prompt)
        self.assertIn("Make add() work. More detail", prompt)

    def test_fails_three_times(self):
        p = make_py_project(self.t, passing=False)
        g, ev = self.gate(p, [{"say": "trying"}])
        o = g.run("x")
        self.assertEqual(o["status"], "failed")
        self.assertEqual(len(o["rounds"]), 4)
        self.assertTrue(o["banner"]["text"].startswith("Failed after 3 fix rounds"))

    def test_deleting_test_flagged(self):
        p = make_py_project(self.t, passing=False)
        g, ev = self.gate(p, {"rounds": [[], [{"delete": "tests/test_calc.py"}, {"write": "tests/test_other.py", "content": "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"}]]})
        o = g.run("x")
        self.assertEqual(o["status"], "needs_review")
        self.assertTrue(any("deleted" in i for i in o["issues"]))
        self.assertEqual(o["banner"]["colour"], "orange")

    def test_skip_marker_flagged(self):
        p = make_py_project(self.t, passing=False)
        src = (p / "tests/test_calc.py").read_text().replace("    def test_add", "    @unittest.skip('later')\n    def test_add")
        g, ev = self.gate(p, {"rounds": [[], [{"write": "tests/test_calc.py", "content": src}]]})
        o = g.run("x")
        self.assertEqual(o["status"], "needs_review")
        self.assertTrue(any("Skip" in i for i in o["issues"]))

    def test_dont_verify(self):
        p = make_py_project(self.t, passing=False)
        g, ev = self.gate(p, [], verify=False)
        self.assertEqual(g.run("x")["status"], "unverified")

    def test_prompt_trimmed(self):
        out = self.t / "big.log"; out.write_text("x" * 200000 + "\nError: boom\n")
        s = {"results": [{"name": "tests", "cmd": "t", "ok": False, "exit_code": 1, "output_file": str(out)}]}
        self.assertLess(len(build_fix_prompt("task", s, 1, 3)), 30000)


class Guard(unittest.TestCase):
    def test_config_and_count(self):
        t = Path(tempfile.mkdtemp())
        (t / "package.json").write_text('{"scripts": {"test": "jest"}, "dependencies": {}}')
        a = guard.snapshot(t, 10)
        (t / "package.json").write_text('{"scripts": {"test": "jest"}, "dependencies": {"x": "1"}}')
        self.assertEqual(guard.compare(a, guard.snapshot(t, 10)), [])   # deps change is fine
        (t / "package.json").write_text('{"scripts": {"test": "echo ok"}}')
        issues = guard.compare(a, guard.snapshot(t, 8))
        self.assertTrue(any("package.json" in i for i in issues))
        self.assertTrue(any("dropped" in i for i in issues))

    def test_js_and_kotlin_skips(self):
        t = Path(tempfile.mkdtemp()); (t / "src/test").mkdir(parents=True)
        (t / "a.test.js").write_text("it('a', () => {})\n")
        (t / "src/test/FooTest.kt").write_text("@Test fun a() {}\n")
        a = guard.snapshot(t)
        (t / "a.test.js").write_text("it.skip('a', () => {})\n")
        (t / "src/test/FooTest.kt").write_text("@Ignore @Test fun a() {}\n")
        self.assertEqual(len(guard.compare(a, guard.snapshot(t))), 2)


if __name__ == "__main__":
    unittest.main()
