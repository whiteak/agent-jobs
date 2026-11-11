import json, tempfile, time, unittest
from datetime import datetime
from pathlib import Path
from agentjobs_upgrades import events, diffparse, actions, auth, notify, bestofn, plan, vision, report
from agentjobs_upgrades.bestofn import Attempt
from tests.helpers import git


class Events(unittest.TestCase):
    def test_add_list_sse(self):
        e = events.JobEvents(Path(tempfile.mkdtemp()) / "ev.jsonl")
        e.add("started", "Started"); e.add("checks", "Checks passed", {"n": 1})
        self.assertEqual([x["id"] for x in e.list()], [1, 2])
        self.assertEqual([x["id"] for x in e.list(after=1)], [2])
        chunk = events.sse_format(e.list()[1])
        self.assertTrue(chunk.startswith("id: 2\nevent: job\ndata: {"))
        self.assertTrue(chunk.endswith("\n\n"))
        g = events.sse_stream(e, last_id=1, poll_s=0.01)
        self.assertEqual(next(g), "retry: 3000\n\n")
        self.assertIn('"id": 2', next(g))

    def test_badge(self):
        self.assertEqual(events.badge("safety_stop"), ("Safety stop", "red"))


DIFF = """diff --git a/a.py b/a.py
index 1..2 100644
--- a/a.py
+++ b/a.py
@@ -1,3 +1,3 @@ def f():
 x = 1
-y = 2
+y = 3
 z = 4
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+hello
+-- not a header
\\ No newline at end of file
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
diff --git a/x.py b/y.py
similarity index 90%
rename from x.py
rename to y.py
diff --git a/app/build/out.apk b/app/build/out.apk
Binary files a/app/build/out.apk and b/app/build/out.apk differ
"""


class Diff(unittest.TestCase):
    def test_parse(self):
        f = diffparse.parse(DIFF)
        s = diffparse.summary(f)
        self.assertEqual([x["status"] for x in s], ["modified", "added", "deleted", "renamed", "modified"])
        self.assertEqual((s[0]["added"], s[0]["removed"]), (1, 1))
        self.assertEqual(s[1]["added"], 2)
        self.assertEqual(f[1].hunks[0].lines[-1][0], "\\")
        self.assertEqual(f[1].hunks[0].lines[1][3], "-- not a header")
        self.assertEqual((s[3]["old_path"], s[3]["path"]), ("x.py", "y.py"))
        self.assertTrue(s[4]["binary"] and s[4]["generated"])
        self.assertEqual(f[0].hunks[0].header, "def f():")
        rows = diffparse.side_by_side(f[0].hunks[0])
        self.assertEqual(rows[1], ((2, "-", "y = 2"), (2, "+", "y = 3")))

    def test_generated(self):
        for p in ("node_modules/a.js", "app/build/x", "package-lock.json", "img.PNG"):
            self.assertTrue(diffparse.is_generated(p), p)
        self.assertFalse(diffparse.is_generated("src/build_utils.py"))


class Actions(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.r = self.t / "r"; self.r.mkdir()
        git(self.r, "init", "-q", "-b", "main")
        (self.r / "a.txt").write_text("1\n"); git(self.r, "add", "-A"); git(self.r, "commit", "-qm", "base")
        git(self.r, "checkout", "-qb", "agent/j1")
        (self.r / "b.txt").write_text("b\n"); git(self.r, "add", "-A"); git(self.r, "commit", "-qm", "job")
        git(self.r, "checkout", "-q", "main")
        self.d = actions.Decisions(self.r)

    def test_secret_and_token(self):
        s = actions.load_secret(self.t / "sec/secret")
        self.assertEqual(oct((self.t / "sec/secret").stat().st_mode & 0o777), "0o600")
        tok = actions.form_token(s, "sid")
        self.assertTrue(actions.check_request(s, "POST", "sid", tok))
        self.assertFalse(actions.check_request(s, "GET", "sid", tok))
        self.assertFalse(actions.check_request(s, "POST", "other", tok))
        self.assertFalse(actions.check_request(s, "POST", None, None))

    def test_merge_gate(self):
        self.assertEqual(actions.merge_allowed("failed")[0], False)
        self.assertEqual(actions.merge_allowed("failed", "MERGE")[0], True)
        self.assertEqual(actions.merge_allowed("passed")[0], True)

    def test_ff_and_undo(self):
        r = self.d.merge("j1", "agent/j1", "main", "T")
        self.assertEqual((r["ok"], r["how"], r["pushed"]), (True, "fast-forward", False))
        self.assertTrue((self.r / "b.txt").exists())
        self.assertTrue(self.d.undo_merge(r)["ok"])
        self.assertFalse((self.r / "b.txt").exists())

    def test_merge_commit(self):
        (self.r / "c.txt").write_text("c\n"); git(self.r, "add", "-A"); git(self.r, "commit", "-qm", "main moved")
        r = self.d.merge("j1", "agent/j1", "main", "Title")
        self.assertEqual(r["how"], "merge commit")
        self.assertIn("Merge agent job j1: Title", git(self.r, "log", "-1", "--format=%s"))

    def test_conflict_leaves_everything(self):
        git(self.r, "checkout", "-q", "agent/j1"); (self.r / "a.txt").write_text("job\n"); git(self.r, "commit", "-qam", "j")
        git(self.r, "checkout", "-q", "main"); (self.r / "a.txt").write_text("main\n"); git(self.r, "commit", "-qam", "m")
        head = git(self.r, "rev-parse", "HEAD")
        r = self.d.merge("j1", "agent/j1", "main", "T")
        self.assertTrue(r.get("conflict"))
        self.assertEqual(head, git(self.r, "rev-parse", "HEAD"))
        self.assertEqual(git(self.r, "status", "--porcelain"), "")

    def test_reject_unreject(self):
        rec = self.d.reject("j1", "agent/j1")
        self.assertIn("agent/rejected/j1", git(self.r, "branch"))
        self.assertTrue(self.d.unreject(rec)["ok"])
        self.assertIn("agent/j1", git(self.r, "branch"))


class Auth(unittest.TestCase):
    def test_password(self):
        h = auth.hash_password("pw", iterations=1000)
        self.assertTrue(auth.verify_password("pw", h))
        self.assertFalse(auth.verify_password("nope", h))
        self.assertNotIn("pw$", h)

    def test_lockout(self):
        t = [0.0]
        lo = auth.Lockout(now=lambda: t[0])
        for _ in range(5):
            lo.fail("1.2.3.4")
        self.assertTrue(lo.locked("1.2.3.4"))
        t[0] += 301
        self.assertFalse(lo.locked("1.2.3.4"))

    def test_sessions_and_local(self):
        s = auth.Sessions(Path(tempfile.mkdtemp()) / "s.json")
        sid = s.create()
        self.assertTrue(s.valid(sid))
        self.assertFalse(auth.needs_login("127.0.0.1", None, s))
        self.assertTrue(auth.needs_login("192.168.1.5", None, s))
        self.assertFalse(auth.needs_login("192.168.1.5", sid, s))
        self.assertIn("HttpOnly", auth.cookie_header("aj", sid))


class Notify(unittest.TestCase):
    def test_quiet_and_send(self):
        s = notify.NotifySettings(server="http://pc:8099")
        self.assertEqual(notify.validate(s), [])
        sent = []
        post = lambda url, body, tok: sent.append(body)
        msg = notify.build("job_finished", "Job finished ✓", ["42 tests pass", "line2", "line3"], "j1", s)
        self.assertFalse(notify.send(s, "job_finished", msg, now=datetime(2026, 1, 1, 23, 0), post=post))
        self.assertTrue(notify.send(s, "safety_stop", msg, now=datetime(2026, 1, 1, 23, 0), post=post))
        self.assertTrue(notify.send(s, "job_finished", msg, now=datetime(2026, 1, 1, 12, 0), post=post))
        self.assertEqual(sent[0]["topic"], s.topic)
        self.assertNotIn("line3", sent[0]["message"])

    def test_clean_and_retry(self):
        self.assertNotIn("/home", notify.clean("Failed in /home/jason/agent-work/x.py: `assert x`"))
        s = notify.NotifySettings(server="http://pc", quiet_start="00:00", quiet_end="00:01")
        calls = []
        def post(*a):
            calls.append(1); raise OSError("down")
        self.assertFalse(notify.send(s, "job_finished", {"title": "t"}, now=datetime(2026, 1, 1, 12), post=post,
                                     sleep=lambda x: None, log=lambda m: None))
        self.assertEqual(len(calls), 3)

    def test_off_and_validation(self):
        self.assertFalse(notify.send(notify.NotifySettings(), "job_finished", {}))
        self.assertTrue(notify.validate(notify.NotifySettings(server="ftp://x", topic="a", quiet_start="25:00")))


class BestOfN(unittest.TestCase):
    def test_winner_tiebreaks(self):
        a = [Attempt(1, "t1", "passed", 10, 0, 50, 100), Attempt(2, "t2", "passed", 12, 0, 80, 200),
             Attempt(3, "t3", "failed", 20)]
        w, r = bestofn.pick_winner(a)
        self.assertEqual(w.k, 2)
        self.assertIn("more tests", bestofn.why(w, r))
        a = [Attempt(1, "t1", "passed", 10, 2, 50), Attempt(2, "t2", "passed", 10, 0, 80)]
        self.assertEqual(bestofn.pick_winner(a)[0].k, 2)
        a = [Attempt(1, "t1", "passed", 10, 0, 90), Attempt(2, "t2", "passed", 10, 0, 30)]
        self.assertEqual(bestofn.pick_winner(a)[0].k, 2)
        a = [Attempt(1, "t1", "passed", 10, issues=["skip added"]), Attempt(2, "t2", "failed")]
        self.assertIsNone(bestofn.pick_winner(a)[0])

    def test_continue(self):
        ok = [Attempt(1, "t", "passed")]
        self.assertFalse(bestofn.should_continue(ok, 3, hard=False))
        self.assertTrue(bestofn.should_continue(ok, 3, hard=True))
        self.assertFalse(bestofn.should_continue([Attempt(1, "t", "safety_stop")], 3, hard=True))


T = lambda i, deps=(), m="M1": {"id": i, "milestone": m, "title": i, "prompt": "do " + i, "acceptance": ["works"], "depends_on": list(deps)}


class Plan(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(plan.validate([T("a"), T("b", ["a"])]), [])
        self.assertTrue(any("unknown task" in e for e in plan.validate([T("a", ["zz"])])))
        self.assertTrue(any("duplicate" in e for e in plan.validate([T("a"), T("a")])))
        self.assertTrue(any("circular" in e for e in plan.validate([T("a", ["b"]), T("b", ["a"])])))
        bad = dict(T("a"), size="XL", extra=1); bad["acceptance"] = []
        errs = plan.validate([bad])
        self.assertTrue(any("size" in e for e in errs) and any("unknown field" in e for e in errs) and any("acceptance" in e for e in errs))

    def test_order_stable(self):
        o = plan.order([T("c", ["b"]), T("a"), T("b", ["a"]), T("d")])
        self.assertEqual([t["id"] for t in o], ["a", "b", "c", "d"])

    def test_queue_stop_skip_recover_cutoff(self):
        d = Path(tempfile.mkdtemp())
        q = plan.Queue(d / "q.json")
        q.load_plan([T("a"), T("b", ["a"]), T("c")], on_fail="skip")
        t = q.next_task(); self.assertEqual(t["id"], "a")
        q.mark("a", "running", "job1")
        q2 = plan.Queue(d / "q.json"); self.assertEqual(q2.recover(), ["a"])
        self.assertEqual(q2.next_task()["id"], "a")
        q2.mark("a", "failed")
        self.assertEqual(q2._by_id()["b"]["state"], "skipped")
        self.assertEqual(q2.next_task()["id"], "c")
        q2.mark("c", "done")
        self.assertEqual(q2.state["status"], "finished")
        q3 = plan.Queue(d / "q3.json"); q3.load_plan([T("a"), T("b", ["a"])])
        q3.mark("a", "failed"); self.assertEqual(q3.state["status"], "stopped"); self.assertIsNone(q3.next_task())
        now = [datetime(2026, 1, 1, 23, 0).timestamp()]
        q4 = plan.Queue(d / "q4.json", now=lambda: now[0]); q4.load_plan([T("a")])
        q4.set_cutoff("06:30")
        self.assertIsNotNone(q4.next_task())
        now[0] = datetime(2026, 1, 2, 6, 31).timestamp()
        self.assertIsNone(q4.next_task())
        self.assertEqual(q4.progress(), [{"milestone": "M1", "done": 0, "total": 1}])

    def test_job_prompt(self):
        p = plan.job_prompt("App that X", "M1: basics", T("a"))
        self.assertIn("AGENT_NOTES.md", p); self.assertIn("- works", p)


class Vision(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(vision.parse_verdict('blah\n{"pass": true, "issues": [], "confidence": 0.8}')["pass"], True)
        self.assertEqual(vision.parse_verdict("<think>{\"pass\": true}</think> {'pass': False, 'issues': ['no button']}")["issues"], ["no button"])
        self.assertIsNone(vision.parse_verdict("looks fine to me"))

    def test_votes(self):
        seq = iter(['{"pass": true}', '{"pass": false, "issues": ["x"]}'])
        self.assertEqual(vision.check(b"png", "spec", lambda b: next(seq))["verdict"], "inconclusive")
        self.assertEqual(vision.check(b"png", "spec", lambda b: '{"pass": false, "issues": ["x"]}')["issues"], ["x"])
        self.assertEqual(vision.check(b"png", "spec", lambda b: '{"pass": true}')["verdict"], "pass")
        self.assertEqual(vision.check(b"png", "spec", lambda b: "garbage")["verdict"], "inconclusive")
        req = vision.build_request(b"\x89PNG", "red button")
        self.assertTrue(req["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        r = vision.to_check_result("looks", {"verdict": "fail", "issues": ["x"]})
        self.assertEqual((r["ok"], r["mode"]), (False, "advisory"))


class Report(unittest.TestCase):
    def test_report(self):
        now = time.time()
        jobs = [
            {"id": "a", "title": "Login screen", "project": "Notes app", "status": "passed", "started": now - 3600, "finished": now - 3000, "tests_passed": 12, "approval": "waiting", "rounds": 1},
            {"id": "b", "title": "Sync", "project": "Notes app", "status": "failed", "started": now - 2900, "finished": now - 1000, "first_failure": "AssertionError: sync\nmore", "queue_blocked": True, "rounds": 3},
            {"id": "c", "title": "Bad", "status": "safety_stop", "started": now - 900, "finished": now - 800, "safety_command": "curl https://x"},
            {"id": "old", "title": "Old", "status": "passed", "started": now - 99999, "finished": now - 99000},
        ]
        called = []
        md = report.build(jobs, [{"label": "draft6", "finished": now - 10, "verdict": "No real difference"}],
                          since=now - 7200, until=now, base_url="http://pc:8090",
                          qwen_summary=lambda facts: called.append(facts) or "All good mostly.")
        for s in ("Needs you", "waiting for your approval", "blocking its queue", "Safety stop: curl https://x",
                  "AssertionError: sync", "draft6: No real difference", "Qwen's summary", "(12 tests)", "/jobs/a"):
            self.assertIn(s, md)
        self.assertNotIn("Old", md)
        md2 = report.build(jobs, [], since=now - 7200, until=now, qwen_summary=lambda f: None)
        self.assertNotIn("Qwen's summary", md2)


if __name__ == "__main__":
    unittest.main()
