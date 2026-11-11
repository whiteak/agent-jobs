import json, sys, tempfile, unittest
from pathlib import Path
from agentjobs_upgrades import checks as ck
from tests.helpers import make_py_project


class Detect(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())

    def mk(self, files):
        for k, v in files.items():
            p = self.t / k; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(v)
        return ck.detect_checks(self.t)

    def test_gradle(self):
        c = self.mk({"gradlew": "#!/bin/sh"})
        self.assertEqual([x.cmd for x in c], ["./gradlew --offline assembleDebug", "./gradlew --offline testDebugUnitTest"])

    def test_npm(self):
        c = self.mk({"package.json": json.dumps({"scripts": {"test": "node --test", "build": "tsc"}})})
        self.assertEqual([x.cmd for x in c], ["npm run build", "npm test"])

    def test_npm_placeholder_test_ignored(self):
        self.assertEqual(self.mk({"package.json": json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}})}), [])

    def test_python(self):
        c = self.mk({"tests/test_x.py": ""})
        self.assertIn("test", c[0].cmd)

    def test_make(self):
        self.assertEqual(self.mk({"Makefile": "all:\n\ntest:\n\techo ok\n"})[0].cmd, "make test")

    def test_cargo_dotnet(self):
        self.assertEqual(self.mk({"Cargo.toml": ""})[1].cmd, "cargo test --offline")

    def test_nothing(self):
        checks, src = ck.load_checks(self.t)
        self.assertEqual((checks, src), ([], "none"))

    def test_file_wins_and_roundtrip(self):
        ck.write_checks(self.t, [ck.Check("x", "true", 5), ck.Check("v", "", type="vision", extra={"spec": "a"})])
        checks, src = ck.load_checks(self.t)
        self.assertEqual(src, "file")
        self.assertEqual(checks[0].timeout_s, 5)
        self.assertEqual(checks[1].extra["spec"], "a")


class Parse(unittest.TestCase):
    def test_pytest(self):
        self.assertEqual(ck.parse_counts("..F\n===== 1 failed, 40 passed, 2 skipped in 3.2s ====="), (40, 1, 2))

    def test_unittest_ok(self):
        self.assertEqual(ck.parse_counts("....\nRan 4 tests in 0.1s\n\nOK (skipped=1)"), (3, 0, 1))

    def test_unittest_fail(self):
        self.assertEqual(ck.parse_counts("Ran 10 tests in 0.1s\n\nFAILED (failures=2, errors=1)"), (7, 3, 0))

    def test_jest(self):
        self.assertEqual(ck.parse_counts("Tests:       1 failed, 1 skipped, 12 passed, 14 total"), (12, 1, 1))

    def test_node_tap(self):
        self.assertEqual(ck.parse_counts("# tests 5\n# pass 4\n# fail 1\n# skipped 0\n"), (4, 1, 0))

    def test_cargo(self):
        self.assertEqual(ck.parse_counts("test result: ok. 3 passed; 0 failed; 1 ignored;\ntest result: ok. 2 passed; 1 failed; 0 ignored;"), (5, 1, 1))

    def test_junit(self):
        t = Path(tempfile.mkdtemp())
        d = t / "app/build/test-results/testDebugUnitTest"; d.mkdir(parents=True)
        (d / "TEST-a.xml").write_text('<testsuite tests="5" failures="1" errors="0" skipped="1"></testsuite>')
        self.assertEqual(ck.parse_counts("BUILD FAILED", t), (3, 1, 1))

    def test_unknown(self):
        self.assertEqual(ck.parse_counts("hello"), (None, None, None))


class Run(unittest.TestCase):
    def test_pass_and_fail(self):
        t = Path(tempfile.mkdtemp())
        p = make_py_project(t)
        s = ck.run_all(p, t / "out")
        self.assertEqual(s["status"], "passed")
        self.assertEqual(s["results"][0]["passed"], 2)
        self.assertEqual(ck.banner_text(s), ("green", "Checks passed (2 tests)"))
        (p / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        s = ck.run_all(p, t / "out")
        self.assertEqual(s["status"], "failed")
        self.assertEqual(ck.banner_text(s)[0], "red")
        self.assertIn("1 failed", ck.banner_text(s)[1])
        self.assertIn("AssertionError", s["results"][0]["first_error"])

    def test_timeout(self):
        t = Path(tempfile.mkdtemp())
        r = ck.run_check(t, ck.Check("slow", "sleep 30", timeout_s=1), t / "o")
        self.assertTrue(r.timed_out)
        self.assertFalse(r.ok)
        self.assertLess(r.seconds, 15)

    def test_unverified(self):
        t = Path(tempfile.mkdtemp())
        s = ck.run_all(t, t / "o")
        self.assertEqual(s["status"], "unverified")
        self.assertEqual(ck.banner_text(s)[0], "yellow")

    def test_jail_wrap_shape(self):
        w = ck.jail_wrap("/x/agent-jail.sh")
        self.assertEqual(w(Path("/p"), "make test"), ["/x/agent-jail.sh", "--project", "/p", "--", "bash", "-lc", "make test"])

    def test_advisory_does_not_fail(self):
        s = ck.summarize([{"name": "v", "ok": False, "mode": "advisory"}, {"name": "t", "ok": True}], "file")
        self.assertEqual(s["status"], "passed")


if __name__ == "__main__":
    unittest.main()
