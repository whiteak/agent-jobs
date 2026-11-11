"""U01 integration tests: verify gate, checkpoints and watchdog wired into the job board.

Each job runs in a child process with JOBBOARD_JAIL=host (checks, git and the fake agent
run directly on temp folders - no podman, no model) and JOBBOARD_FAKE=1 (hidden fake mode:
the job text is a fakeagent scenario). Nothing touches ~/agent-sandbox or port 8080.
"""
import json, os, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path
from unittest import mock

WEB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB))

import upgrades  # noqa: E402
from agentjobs_upgrades import watchdog as wdm  # noqa: E402

GOOD = "def add(a, b):\n    return int(a + b)\n"
BAD = "def add(a, b):\n    return a - b - 0\n"
TEST = ("import sys, os, unittest\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
        "    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n")
CHECK = [{"name": "tests", "cmd": f"{sys.executable} -m unittest discover -s tests -p 'test_*.py'", "timeout_s": 60}]

DRIVER = textwrap.dedent("""
    import json, sys
    import jobboard
    spec = json.loads(sys.argv[1])
    m = jobboard.new_job("fake", spec["folder"], json.dumps(spec["scenario"]), False, extra=spec.get("extra"))
    jobboard.process_job(m["id"])
    if spec.get("then"):
        kind, args = spec["then"]
        jobboard.u01_button(kind, m["id"], lambda k: str(args.get(k, "")))
    print("JOBID=" + m["id"])
""")


def git(repo, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo,
                          check=True, capture_output=True, text=True).stdout


class Board(unittest.TestCase):
    """Runs one fake job per test through process_job in a child process."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="u01-"))
        self.home = self.t / "home"
        (self.home / "agent-sandbox" / "state").mkdir(parents=True)
        self.work = self.t / "work"
        self.jobs = self.t / "jobs"
        self.work.mkdir()
        self.jobs.mkdir()
        self.settings = self.t / "settings.json"
        self.settings.write_text(json.dumps({"watchdog_tick_s": 1, "after_result_grace_s": 3}))

    def project(self, passing=True, repo=True, name="proj"):
        p = self.work / name
        (p / "tests").mkdir(parents=True)
        (p / "calc.py").write_text(GOOD if passing else BAD)
        (p / "tests" / "test_calc.py").write_text(TEST)
        (p / "PLAN.md").write_text("# Plan\n- [ ] step one\n- [ ] step two\n")
        (p / ".gitignore").write_text("__pycache__/\n")
        if repo:
            git(p, "init", "-q", "-b", "main")
            git(p, "add", "-A")
            git(p, "commit", "-qm", "base")
        return p

    def run_job(self, scenario, extra=None, folder="proj", then=None, timeout=240):
        env = dict(os.environ, HOME=str(self.home), JOBBOARD_JOBS=str(self.jobs), JOBBOARD_WORK=str(self.work),
                   JOBBOARD_SETTINGS=str(self.settings), JOBBOARD_JAIL="host", JOBBOARD_FAKE="1",
                   PYTHONDONTWRITEBYTECODE="1")
        spec = {"folder": folder, "scenario": scenario, "extra": extra if extra is not None else {"checks": CHECK},
                "then": then}
        r = subprocess.run([sys.executable, "-c", DRIVER, json.dumps(spec)], cwd=str(WEB), env=env,
                           capture_output=True, text=True, timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        jid = [l for l in r.stdout.splitlines() if l.startswith("JOBID=")][-1][6:]
        self.jid = jid
        return json.loads((self.jobs / jid / "meta.json").read_text())

    def events(self):
        out = []
        for line in (self.jobs / self.jid / "events.jsonl").read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out


class Gate(Board):
    def test_pass_first_time_is_green(self):
        self.project(passing=True)
        m = self.run_job([{"say": "All done, tests pass."}])
        self.assertEqual(m["status"], "done")
        self.assertEqual(m["verify"]["status"], "passed")
        self.assertEqual(m["verify"]["banner"], {"colour": "green", "text": "Checks passed (2 tests)"})
        s = upgrades.overlay_status({"state": "FINISHED", "label": "Finished", "fail_reason": None}, m)
        self.assertEqual((s["label"], s["tone"]), ("Checks passed (2 tests)", "green"))
        kinds = [e.get("kind") for e in self.events() if e.get("type") == "agentjobs"]
        self.assertIn("checks", kinds)
        self.assertTrue(m["session"].startswith("fake-"))

    def test_fixed_in_round_one(self):
        self.project(passing=False)
        m = self.run_job({"rounds": [[{"say": "I think it works"}], [{"write": "calc.py", "content": GOOD}]]})
        self.assertEqual(m["status"], "done")
        self.assertEqual(m["verify"]["status"], "passed")
        self.assertEqual(m["verify"]["timeline"],
                         "Agent finished → checks failed (1) → fix round 1 → checks passed")
        prompts = list((self.jobs / self.jid / "prompts").glob("*round-1.txt"))
        self.assertEqual(len(prompts), 1)
        txt = prompts[0].read_text()
        self.assertIn("These checks failed: tests", txt)
        self.assertIn("Do not delete, skip or weaken tests", txt)
        # the fix round resumed the agent's own session
        sessions = {e.get("session_id") for e in self.events() if e.get("type") == "system"}
        self.assertEqual(len(sessions), 1)

    def test_fails_after_all_rounds_is_red(self):
        self.project(passing=False)
        m = self.run_job([{"say": "trying hard"}], extra={"checks": CHECK, "fix_rounds": 2})
        self.assertEqual(m["status"], "failed")
        self.assertEqual(m["verify"]["status"], "failed")
        self.assertTrue(m["fail_note"].startswith("Failed after 2 fix rounds"), m["fail_note"])
        self.assertEqual(len(m["verify"]["rounds"]), 3)

    def test_cheating_is_orange_needs_review(self):
        self.project(passing=False)
        other = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"
        m = self.run_job({"rounds": [[], [{"delete": "tests/test_calc.py"},
                                          {"write": "tests/test_other.py", "content": other}]]})
        self.assertEqual(m["verify"]["status"], "needs_review")
        self.assertEqual(m["verify"]["banner"]["colour"], "orange")
        self.assertTrue(any("deleted" in i for i in m["verify"]["issues"]))
        guard = [e for e in self.events() if e.get("kind") == "guard"]
        self.assertTrue(guard)

    def test_dont_verify_is_yellow(self):
        self.project(passing=False)
        m = self.run_job([], extra={"verify_off": True})
        self.assertEqual(m["status"], "done")
        self.assertEqual(m["verify"]["status"], "unverified")
        self.assertEqual(m["verify"]["banner"]["colour"], "yellow")

    def test_no_checks_found_is_not_verified(self):
        p = self.work / "docs"
        p.mkdir()
        (p / "README.md").write_text("hi\n")
        m = self.run_job([{"write": "README.md", "content": "hello\n"}], extra={}, folder="docs")
        self.assertEqual(m["verify"]["status"], "unverified")
        self.assertIn("not verified", m["verify"]["banner"]["text"])
        self.assertIn("not a git repository", m["checkpoints_note"])

    def test_checks_frozen_from_agent_checks_json_at_start(self):
        p = self.project(passing=True)
        (p / "AGENT_CHECKS.json").write_text(json.dumps({"checks": CHECK}))
        git(p, "add", "-A"); git(p, "commit", "-qm", "checks")
        # the agent swaps the check for one that always passes: the frozen one still runs
        m = self.run_job([{"write": "AGENT_CHECKS.json", "content": json.dumps({"checks": [{"name": "t", "cmd": "true"}]})},
                          {"write": "calc.py", "content": BAD}], extra={"fix_rounds": 0})
        self.assertEqual(m["checks_source"], "AGENT_CHECKS.json at job start")
        self.assertEqual(m["verify"]["rounds"][0]["summary"]["results"][0]["name"], "tests")
        self.assertEqual(m["verify"]["status"], "failed")


class Checkpoints(Board):
    def test_checkpoint_then_auto_rollback(self):
        p = self.project(passing=True)
        scen = {"rounds": [[{"tick": 1}, {"sleep": 4}, {"write": "calc.py", "content": BAD}], []]}
        m = self.run_job(scen)
        cp = m["checkpoints"]
        self.assertGreaterEqual(len(cp["checkpoints"]), 1)
        self.assertEqual(cp["checkpoints"][0]["step"], "step one")
        self.assertEqual(cp["rollbacks"], 1)
        self.assertEqual(m["verify"]["status"], "passed")          # passed again after the rollback
        self.assertIn("return int(a + b)", (p / "calc.py").read_text())
        self.assertIn(cp["discarded"][0], git(p, "branch", "--list", "agent/*"))
        txt = next((self.jobs / self.jid / "prompts").glob("*round-1.txt")).read_text()
        self.assertIn("rolled back", txt)
        # the job's checkpoint state lives inside the repo's .git, worktrees are gone
        st = p / ".git" / "agentjobs" / self.jid
        self.assertTrue((st / "checkpoints.json").is_file())
        self.assertEqual(list(st.glob("wt-*")), [])

    def test_manual_roll_back_to_here(self):
        p = self.project(passing=True)
        scen = [{"tick": 1}, {"sleep": 4}, {"write": "extra.py", "content": "print(1)\n"}]
        m = self.run_job(scen, then=["rollback", {"n": 1}])
        self.assertFalse((p / "extra.py").exists(), "rolled back to the checkpoint before extra.py")
        m = json.loads((self.jobs / self.jid / "meta.json").read_text())
        self.assertEqual(len(m["checkpoints"]["discarded"]), 1)
        self.assertIn("print(1)", git(p, "show", m["checkpoints"]["discarded"][0] + ":extra.py"))


class Watchdog(Board):
    def test_safety_stop_kills_the_job(self):
        self.project(passing=True)
        t0 = time.time()
        m = self.run_job([{"cmd": "curl https://example.com"}, {"sleep": 60}, {"say": "never"}])
        self.assertLess(time.time() - t0, 50, "the job must be killed, not run its 60 s sleep")
        self.assertEqual(m["status"], "safety_stop")
        self.assertIn("example.com", m["fail_note"])
        self.assertNotIn("verify", m)                      # no checks after a safety stop
        self.assertEqual(m["watchdog"][0]["kind"], "safety_stop")
        self.assertEqual(m["watchdog"][0]["command"], "curl https://example.com")
        hi = [e for e in self.events() if e.get("kind") == "safety_stop"]
        self.assertEqual(hi[0]["tone"], "bad")
        s = upgrades.overlay_status({"state": "FAILED", "label": "Failed", "fail_reason": None}, m)
        self.assertEqual((s["label"], s["tone"]), ("Safety stop", "red"))

    def test_normal_commands_do_not_trigger(self):
        self.project(passing=True)
        m = self.run_job([{"cmd": "python3 -m unittest -q", "run": False}, {"cmd": "git commit -am x"},
                          {"cmd": "curl -s http://127.0.0.1:8080/v1/models"}, {"say": "done"}])
        self.assertEqual(m["status"], "done")
        self.assertFalse(m.get("watchdog"))


class Pieces(unittest.TestCase):
    def test_result_line_any_key_order(self):
        claude = json.dumps({"duration_api_ms": 5, "is_error": False, "session_id": "s", "type": "result"})
        qwen = json.dumps({"type": "result", "is_error": True})
        self.assertIsNotNone(upgrades.is_result_line(claude))
        self.assertTrue(upgrades.is_result_line(qwen)["is_error"])
        self.assertIsNone(upgrades.is_result_line('{"type":"assistant","result":1}'))

    def test_monitor_stops_agent_lingering_after_result(self):
        t = Path(tempfile.mkdtemp())
        ev = t / "events.jsonl"
        ev.write_text(json.dumps({"duration_ms": 1, "is_error": False, "result": "ok", "type": "result"}) + "\n")
        p = subprocess.Popen(["sleep", "30"], start_new_session=True)
        stopped = []
        mon = upgrades.Monitor(ev, 0, None, local=True, stop=lambda: (stopped.append(1), p.kill()),
                               kill=lambda proc: None, pause=lambda on: None, grace=1, poll_s=0.2)
        rc = mon.run(p)
        self.assertEqual(stopped, [1])
        self.assertTrue(mon.after_result)
        self.assertNotEqual(rc, None)

    def test_feeder_qwen_and_claude(self):
        w = wdm.Watchdog(wdm.WatchdogSettings(), "/work/p")
        f = upgrades.Feeder(w)
        f.line(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "c1", "name": "run_shell_command", "input": {"command": "wget http://x.y/z"}}],
            "usage": {"input_tokens": 12113, "cache_read_input_tokens": 9358, "total_tokens": 12234}}}))
        self.assertEqual(w.tokens, 12113)                       # Qwen: input already includes the cache
        self.assertTrue(w.has_urgent())
        w2 = wdm.Watchdog(wdm.WatchdogSettings(), "/work/p")
        f2 = upgrades.Feeder(w2)
        f2.line(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "/work/p/a.py", "content": "x"}},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "cat ~/.claude-local/CLAUDE.md"}}],
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 10904, "cache_read_input_tokens": 8959}}}))
        self.assertEqual(w2.tokens, 2 + 10904 + 8959)
        self.assertFalse(w2.has_urgent(), "reading the agent's own ~/.claude-local is fine")
        self.assertIn("t2", f2.pending)
        f2.line(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "boom", "is_error": True}]}}))
        self.assertEqual(f2.last_error, "boom")
        self.assertEqual(f2.pending, {})
        f2.line(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "/home/agent/.ssh/id_rsa"}}]}}))
        self.assertTrue(w2.has_urgent())

    def test_wrap_maps_paths_into_the_jail(self):
        root = Path(tempfile.mkdtemp()) / "proj"
        (root / ".git" / "agentjobs" / "j1" / "wt-1").mkdir(parents=True)
        with mock.patch.object(upgrades, "HOST_MODE", False):
            w = upgrades.make_wrap(root, "proj", "agentcheck-j1")
            argv = w(root / ".git/agentjobs/j1/wt-1", f"env GIT_INDEX_FILE={root}/.git/x git status")
            self.assertEqual(argv[:4], [str(upgrades.RUN_LOCAL), "sandbox-run", str(root), "proj"])
            self.assertTrue(argv[4].startswith("agentcheck-j1-"))
            self.assertEqual(argv[5:7], ["--", "-lc"])
            self.assertIn("cd /work/proj/.git/agentjobs/j1/wt-1 ", argv[7])
            self.assertIn("GIT_INDEX_FILE=/work/proj/.git/x", argv[7])
            self.assertNotIn(str(root), argv[7])
            with self.assertRaises(ValueError):
                w(root.parent, "ls")

    def test_settings_defaults_and_override(self):
        t = Path(tempfile.mkdtemp()) / "s.json"
        cfg = upgrades.load_settings(t)
        self.assertTrue(t.exists())
        self.assertEqual(cfg["fix_rounds"], 3)
        t.write_text(json.dumps({"fix_rounds": 1, "watchdog": {"max_wall_s": 60, "bogus": 1}}))
        cfg = upgrades.load_settings(t)
        ws = upgrades.watchdog_settings(cfg)
        self.assertEqual((cfg["fix_rounds"], ws.max_wall_s, ws.idle_nudge_s), (1, 60, 900))
        self.assertEqual(ws.allowed_urls[0], "http://127.0.0.1:8080")

    def test_form_checks_and_record_check(self):
        c = upgrades.form_checks("python3 -m unittest\n\n./gradlew --offline test\n")
        self.assertEqual([x["cmd"] for x in c], ["python3 -m unittest", "./gradlew --offline test"])
        self.assertTrue(upgrades.checks_ok(c))
        self.assertIsNone(upgrades.form_checks("  \n"))
        self.assertFalse(upgrades.checks_ok([{"name": "x", "cmd": ""}]))
        self.assertFalse(upgrades.checks_ok([{"name": "x", "cmd": "a", "timeout_s": "9"}]))
        self.assertFalse(upgrades.checks_ok("rm -rf /"))

    def test_bundle_section(self):
        m = {"verify": {"status": "needs_review", "banner": {"colour": "orange", "text": "Needs review: x"},
                        "timeline": "Agent finished → checks failed (1) → fix round 1 → checks passed → needs review",
                        "rounds": [{"round": 0, "summary": {"status": "failed", "results": [
                            {"name": "tests", "cmd": "make test", "ok": False, "exit_code": 1, "failed": 1}]}},
                            {"round": 1, "summary": {"status": "passed", "results": []},
                             "issues": ["Test file deleted: tests/test_a.py"]}]},
             "checkpoints": {"checkpoints": [{"n": 1, "step": "s", "passed": 2, "commit": "abcdef1234"}],
                             "rollbacks": 1, "discarded": ["agent/j-discarded-1"]},
             "watchdog": [{"kind": "safety_stop", "reason": "Safety stop: ran a network tool (curl)", "command": "curl x"}]}
        md = upgrades.bundle_section(m)
        for s in ("## Verify gate", "fix round 1", "CHEATING GUARD: Test file deleted", "## Checkpoints",
                  "agent/j-discarded-1", "## Watchdog", "`curl x`"):
            self.assertIn(s, md)


if __name__ == "__main__":
    unittest.main()
