#!/usr/bin/env python3
"""Upgrade 7a: timeline, Server-Sent Events, live-log parser, badges.

Reuses the jobboard module that test_jobstatus.py imports (read-only, temp folders).
No model server, no agent.
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import http.server
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_jobstatus as tj  # noqa: E402  (imports jobboard with isolated folders)

jobboard = tj.jobboard
import dashboard  # noqa: E402
import upgrades  # noqa: E402

SAMPLES = Path(ROOT) / "sample-jobs"


def parse_sse(chunks):
    """[(id or None, event, data)] from SSE text chunks (comments and retry lines dropped)."""
    out = []
    for block in "".join(chunks).split("\n\n"):
        ev = {"id": None, "event": None, "data": None}
        for line in block.splitlines():
            if line.startswith("id: "): ev["id"] = int(line[4:])
            elif line.startswith("event: "): ev["event"] = line[7:]
            elif line.startswith("data: "): ev["data"] = json.loads(line[6:])
        if ev["event"]:
            out.append((ev["id"], ev["event"], ev["data"]))
    return out


class Stream(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="u07-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.meta = {"id": "j1", "status": "running"}
        self.clock = [1000.0]

    def stream(self, last=0, off=0, log=None, **kw):
        def sleep(s):
            self.clock[0] += s
        return dashboard.job_stream(
            "j1", last, off, get_meta=lambda jid: self.meta, get_status=lambda m: {"state": m["status"].upper()},
            get_log=log or (lambda jid, o: ([], o)), job_dir=self.dir,
            now=lambda: self.clock[0], sleep=sleep, **kw)

    def test_sse_format(self):
        s = dashboard.sse("job", {"id": 3, "text": "héllo\nline"}, 3)
        self.assertTrue(s.startswith("id: 3\nevent: job\ndata: "))
        self.assertTrue(s.endswith("\n\n"))
        body = s.split("data: ", 1)[1].strip()
        self.assertNotIn("\n", body)                      # one data line, newlines escaped
        self.assertEqual(json.loads(body)["text"], "héllo\nline")
        self.assertNotIn("id:", dashboard.sse("end", {}))

    def test_stream_sends_timeline_log_status_then_end(self):
        dashboard.tl_add(self.dir, "queued", "Queued")
        dashboard.tl_add(self.dir, "started", "Started")
        self.meta["status"] = "done"
        logs = iter([([{"k": "text", "t": "hi"}], 10)])
        chunks = list(self.stream(log=lambda jid, o: next(logs, ([], o))))
        self.assertEqual(chunks[0], "retry: 3000\n\n")
        evs = parse_sse(chunks)
        self.assertEqual([(i, e) for i, e, _ in evs if e == "job"], [(1, "job"), (2, "job")])
        log = [d for _, e, d in evs if e == "log"]
        self.assertEqual(log, [{"from": 0, "next": 10, "items": [{"k": "text", "t": "hi"}]}])
        self.assertEqual([d for _, e, d in evs if e == "status"], [{"state": "DONE"}])   # sent once, not every poll
        self.assertEqual(evs[-1][1:], ("end", {"status": "done"}))

    def test_reconnect_with_last_event_id_only_sends_newer(self):
        for i in range(5):
            dashboard.tl_add(self.dir, "step", f"step {i + 1}")
        self.meta["status"] = "done"
        ids = [i for i, e, _ in parse_sse(self.stream(last=3)) if e == "job"]
        self.assertEqual(ids, [4, 5])

    def test_events_added_while_streaming_arrive_and_keepalive(self):
        g = self.stream(keepalive_s=2.0)
        seen = []
        for n, chunk in enumerate(g):
            seen.append(chunk)
            if n == 1:
                dashboard.tl_add(self.dir, "checks", "Checks passed", {"colour": "green"})
            if n == 8:
                self.meta["status"] = "failed"
        evs = parse_sse(seen)
        self.assertIn((1, "job"), [(i, e) for i, e, _ in evs])
        self.assertTrue(any(c.startswith(": keepalive") for c in seen))
        self.assertEqual(evs[-1][1], "end")

    def test_gone_job_ends(self):
        self.meta = None
        self.assertEqual(parse_sse(self.stream())[-1][1:], ("end", {"reason": "gone"}))

    def test_tl_add_never_raises(self):
        self.assertIsNone(dashboard.tl_add("/proc/nope/nope", "queued", "x"))

    def test_eventlog_writes_timeline(self):
        log = upgrades.EventLog(self.dir / "events.jsonl")
        log("checks", "Checks passed (2 tests)", data={"colour": "green"})
        log("checks_start", "Running the checks")           # not a timeline event
        log("safety_stop", "Safety stop: too many deletes")
        tl = dashboard.timeline_for(self.dir).list()
        self.assertEqual([(e["id"], e["type"]) for e in tl], [(1, "checks"), (2, "safety_stop")])
        h = dashboard.timeline_card(self.dir)
        self.assertIn('data-last="2"', h)
        self.assertIn('class="ok"', h)
        self.assertIn("Safety stop: too many deletes", h)


class LogParser(unittest.TestCase):
    """jobboard.events on the real logs in web/sample-jobs."""

    @classmethod
    def setUpClass(cls):
        for d in SAMPLES.iterdir():
            dst = jobboard.JOBS / d.name
            if not dst.is_dir():
                shutil.copytree(d, dst)

    def test_qwen_log_rows(self):
        items, nxt = jobboard.events("20261107-180819-71a7")
        self.assertEqual(nxt, (SAMPLES / "20261107-180819-71a7" / "events.jsonl").stat().st_size)
        tools = [i for i in items if i["k"] == "tool"]
        # the order the sample log really has: glob, write hello.sh, chmod, git probe, write AGENT_REPORT.md
        self.assertEqual([(t["name"], t["cat"]) for t in tools],
                         [("glob", "read"), ("write_file", "edit"), ("run_shell_command", "cmd"),
                          ("run_shell_command", "cmd"), ("write_file", "edit")])
        self.assertEqual(sorted(t["cat"] for t in tools), ["cmd", "cmd", "edit", "edit", "read"])
        self.assertEqual(len([i for i in items if i["k"] == "text"]), 3)
        self.assertEqual(len([i for i in items if i["k"] == "result"]), 5)
        self.assertEqual(items[-1]["k"], "final")

    def test_claude_log_rows(self):
        items, _ = jobboard.events("20261108-215755-eb6e")
        cats = {}
        for i in items:
            if i["k"] == "tool":
                cats.setdefault(i["cat"], set()).add(i["name"])
        self.assertEqual(cats, {"read": {"Read"}, "cmd": {"Bash"}, "edit": {"Edit", "Write"}})
        self.assertEqual(sum(1 for i in items if i["k"] == "tool"), 36)
        self.assertEqual(sum(1 for i in items if i["k"] == "result"), 36)
        self.assertTrue(all(len(i["t"]) <= 20100 for i in items if i["k"] == "result"))

    def test_resume_from_offset_and_partial_line(self):
        jid = "20261107-180819-71a7"
        all_items, end = jobboard.events(jid)
        raw = (SAMPLES / jid / "events.jsonl").read_bytes()
        cut = raw.index(b"\n", len(raw) // 2) + 1
        tmp = jobboard.JOBS / "u07-partial"
        tmp.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "events.jsonl").write_bytes(raw[:cut] + raw[cut:cut + 7])     # half a line at the end
        a, off = jobboard.events("u07-partial")
        self.assertEqual(off, cut)                                        # the half line is not consumed
        (tmp / "events.jsonl").write_bytes(raw)
        b, off2 = jobboard.events("u07-partial", off)
        self.assertEqual(off2, end)
        self.assertEqual(a + b, all_items)

    def test_long_output_is_sent_whole_for_show_more(self):
        tmp = jobboard.JOBS / "u07-long"
        tmp.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        body = "\n".join(f"line {i}" for i in range(200))
        (tmp / "events.jsonl").write_text(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": body}, {"type": "tool_result", "content": "x" * 30000}]}}) + "\n")
        items, _ = jobboard.events("u07-long")
        self.assertEqual(items[0]["t"], body)
        self.assertTrue(items[1]["t"].endswith("(cut - see the raw log)"))
        self.assertLess(len(items[1]["t"]), 20100)

    def test_tool_categories(self):
        self.assertEqual(jobboard.tool_cat("Bash"), "cmd")
        self.assertEqual(jobboard.tool_cat("run_shell_command"), "cmd")
        self.assertEqual(jobboard.tool_cat("Edit"), "edit")
        self.assertEqual(jobboard.tool_cat("write_file"), "edit")
        self.assertEqual(jobboard.tool_cat("Read"), "read")
        self.assertEqual(jobboard.tool_cat("WebFetch"), "other")


class Badges(unittest.TestCase):
    def b(self, status, verify=None, s=None, **kw):
        m = dict({"status": status}, **kw)
        if verify:
            m["verify"] = {"status": verify}
        return dashboard.badge(m, s)

    def test_the_seven_badges(self):
        self.assertEqual(self.b("running"), ("Working", "blue"))
        self.assertEqual(self.b("stopping"), ("Working", "blue"))
        self.assertEqual(self.b("queued"), ("Queued", "grey"))
        self.assertEqual(self.b("done", "passed"), ("Done ✓", "green"))
        self.assertEqual(self.b("failed", "failed"), ("Failed ✗", "red"))
        self.assertEqual(self.b("failed"), ("Failed ✗", "red"))
        self.assertEqual(self.b("done", "unverified"), ("Not verified", "yellow"))
        self.assertEqual(self.b("done", "needs_review"), ("Needs review", "orange"))
        self.assertEqual(self.b("safety_stop", "failed"), ("Safety stop", "red"))

    def test_other_states(self):
        self.assertEqual(self.b("done"), ("Done", "green"))             # jobs from before the verify gate
        self.assertEqual(self.b("running", s={"state": "STUCK"}), ("Stuck", "orange"))
        self.assertEqual(self.b("stopped")[0], "Stopped")
        self.assertEqual(self.b("cancelled")[0], "Stopped")
        self.assertEqual(self.b("interrupted")[0], "Interrupted")

    def test_checks_cell(self):
        c = dashboard.checks_cell
        res = {"status": "passed", "results": [{"passed": 40}, {"passed": 2}]}
        self.assertEqual(c({"status": "done", "verify": {"status": "passed", "final": res}}), "42 passed")
        bad = {"status": "failed", "results": [{"passed": 41, "failed": 1}]}
        self.assertEqual(c({"status": "failed", "verify": {"status": "failed", "final": bad}}), "1 failed")
        self.assertEqual(c({"status": "done", "verify": {"status": "needs_review", "final": res}}), "needs review")
        self.assertEqual(c({"status": "done", "verify_off": True}), "not checked")
        self.assertEqual(c({"status": "running"}), "...")
        self.assertEqual(c({"status": "done"}), "-")
        self.assertEqual(c({"kind": "bench", "status": "done"}), "-")
        # a later "Re-run checks" result wins
        self.assertEqual(c({"status": "done", "verify": {"status": "failed", "final": bad},
                            "recheck": {"summary": res}}), "42 passed")


class Pages(unittest.TestCase):
    """The real handler on a free port: stream endpoint, raw log, list badges."""

    @classmethod
    def setUpClass(cls):
        LogParser.setUpClass()
        cls.jid = "20261107-180819-71a7"
        dashboard.tl_add(jobboard.JOBS / cls.jid, "queued", "Queued")
        dashboard.tl_add(jobboard.JOBS / cls.jid, "finished", "Finished")
        s = socket.socket(); s.bind(("127.0.0.1", 0)); cls.port = s.getsockname()[1]; s.close()
        cls.saved_port = jobboard.PORT
        jobboard.PORT = cls.port            # guard() reads this at request time (as in test_jobweb_bench)
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), jobboard.H)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close(); cls.thr.join(timeout=5)
        jobboard.PORT = cls.saved_port

    def get(self, path, cookie=True, headers=None):
        import urllib.request, urllib.error
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        if cookie:
            req.add_header("Cookie", f"jb={jobboard.TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")

    def test_stream_endpoint(self):
        code, hdr, body = self.get(f"/api/jobs/{self.jid}/events")
        self.assertEqual(code, 200)
        self.assertTrue(hdr["Content-Type"].startswith("text/event-stream"))
        evs = parse_sse([body])
        self.assertEqual([i for i, e, _ in evs if e == "job"], [1, 2])
        self.assertTrue(any(e == "log" and d["items"] for _, e, d in evs))
        self.assertTrue(any(e == "status" and d.get("badge") for _, e, d in evs))
        self.assertEqual(evs[-1][1], "end")

    def test_stream_resumes_with_last_event_id_and_log_offset(self):
        size = (jobboard.JOBS / self.jid / "events.jsonl").stat().st_size
        code, _, body = self.get(f"/api/jobs/{self.jid}/events?log={size}", headers={"Last-Event-ID": "1"})
        evs = parse_sse([body])
        self.assertEqual([i for i, e, _ in evs if e == "job"], [2])
        self.assertFalse([1 for _, e, _ in evs if e == "log"])

    def test_stream_needs_login_and_a_real_job(self):
        self.assertIn(self.get(f"/api/jobs/{self.jid}/events", cookie=False)[0], (401, 403))
        self.assertEqual(self.get("/api/jobs/nope/events")[0], 404)
        self.assertEqual(self.get("/api/jobs/..%2F..%2Fetc/events")[0], 404)

    def test_raw_log(self):
        code, hdr, body = self.get(f"/api/raw/{self.jid}")
        self.assertEqual(code, 200)
        self.assertTrue(hdr["Content-Type"].startswith("text/plain"))
        self.assertIn('"type"', body)
        self.assertIn(self.get(f"/api/raw/{self.jid}", cookie=False)[0], (401, 403))

    def test_job_page_has_timeline_log_and_keeps_banner(self):
        code, _, h = self.get(f"/job/{self.jid}")
        self.assertEqual(code, 200)
        for needle in ("<h2>Timeline</h2>", "<h2>Live log</h2>", "Search the log", "Show raw log",
                       'id="banner"', "EventSource", "/api/status/", "Enable alerts"):
            self.assertIn(needle, h)

    def test_list_has_checks_column_and_badge(self):
        code, _, h = self.get("/")
        self.assertIn("<th>Checks</th>", h)
        code, _, rows = self.get("/api/rows")
        self.assertIn('class="chk"', rows)
        code, _, st = self.get("/api/statuses")
        s = json.loads(st)["statuses"][self.jid]
        self.assertIn("badge", s)
        self.assertIn("checks", s)


if __name__ == "__main__":
    unittest.main()
