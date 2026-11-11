#!/usr/bin/env python3
"""Tests for the bench auto-run integration in jobboard.py.

Two halves:

* BenchPagesInProcess - reuses the jobboard module that test_jobstatus.py already
  imported (read-only, temp folders) and serves it on its own free port. Checks
  the new pages/endpoints render, the login guard covers every bench route, POSTs
  are refused in read-only mode, bench rows/pills look right, and every bench
  page script passes node --check.

* EndToEndBoard - starts a REAL board process (worker running, read-only off)
  against the fake model server and bench-ref: queue order (a bench run waits
  behind a running agent job and never runs at the same time), the tamper test
  (worker settings come only from BENCH_RUNS/<id>/spec.json, never from the
  job's meta.json), stop -> partial samples kept -> resume -> complete, a full
  before/after comparison through the compare page, and delete-to-trash.

The real Basalt server on 127.0.0.1:8080 is never contacted on purpose (we run
on the model it measures). The board's own header probe of it fails with
"connection refused" here, exactly like the existing test_jobstatus pages.
"""
import http.server
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

if "jobboard" not in sys.modules:  # standalone run: same isolated env test_jobstatus sets up
    _hb = tempfile.mkdtemp(prefix="jbweb-home-")
    os.environ["HOME"] = _hb
    os.environ["JOBBOARD_JOBS"] = os.path.join(_hb, "jobs")
    os.environ["JOBBOARD_WORK"] = os.path.join(_hb, "work")
    os.environ["JOBBOARD_READONLY"] = "1"
import jobboard  # noqa: E402

from fakeserver import FakeServer  # noqa: E402

BENCH_REF = str(ROOT / "bench-ref")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def fetch(port, token, path, data=None, cookie=True, origin=None):
    """-> (status, headers, body). data: dict -> urlencoded POST."""
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data, doseq=True).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body,
                                 method="POST" if body is not None else "GET")
    if cookie:
        req.add_header("Cookie", f"jb={token}")
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if origin is not False:
            req.add_header("Origin", origin or f"http://127.0.0.1:{port}")
    try:
        with OPENER.open(req, timeout=30) as r:
            return r.status, r.headers, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8", "replace")


# ------------------------------------------------------------------ in-process pages

class BenchPagesInProcess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (jobboard.JOBS, jobboard.WORK, jobboard.BENCH_RUNS,
                     jobboard.BENCH_DIR, jobboard.PORT)
        cls.tmp = Path(tempfile.mkdtemp(prefix="jbweb-"))
        jobboard.JOBS = cls.tmp / "jobs"; jobboard.JOBS.mkdir()
        jobboard.WORK = cls.tmp / "work"; jobboard.WORK.mkdir()
        jobboard.BENCH_RUNS = cls.tmp / "runs"; jobboard.BENCH_RUNS.mkdir()
        jobboard.BENCH_DIR = BENCH_REF
        cls.port = free_port()
        jobboard.PORT = cls.port            # guard() reads this at request time
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), jobboard.H)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

        cls.done_a = cls.make_run("before: slow ctx", "baseline run",
                                  ["h2_cron", "p1_expr"], 3, passed=1,
                                  env_cfg={"models.0.max-context": 262144})
        cls.done_b = cls.make_run("after: faster ctx", "raised max-context",
                                  ["h2_cron", "p1_expr"], 3, passed=4,
                                  env_cfg={"models.0.max-context": 131072})
        cls.stopped = cls.make_run("stopped run", "", ["h2_cron"], 3, passed=2,
                                   status="stopped")
        cls.running = cls.make_run("running run", "", ["h2_cron", "p1_expr"], 3,
                                   passed=2, status="running", progress=(4, 6))
        cls.queued = cls.make_run("queued run", "", ["h2_cron"], 1, passed=1,
                                  status="queued", progress=(0, 1), samples_written=0)
        # exit 3: the server stopped answering mid-run, so the run pauses at "failed"
        cls.failed = cls.make_run("paused run", "", ["h2_cron"], 3, passed=2,
                                  status="failed", samples_written=2,
                                  fail_note="Basalt stopped answering - press Resume "
                                            "when it is back")
        # 4 samples: 1 request failed (a line from the old scoring) and 3 were cut
        cls.truncated = cls.make_run("cut answers run", "", ["h2_cron"], 4, passed=2,
                                     cut=3, errors=1)
        # same model/config as done_a, only the token limit differs
        cls.wider = cls.make_run("bigger token limit", "only max_tokens changed",
                                 ["h2_cron", "p1_expr"], 3, passed=1, max_tokens=32768)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thr.join(timeout=5)
        (jobboard.JOBS, jobboard.WORK, jobboard.BENCH_RUNS,
         jobboard.BENCH_DIR, jobboard.PORT) = cls.saved

    @classmethod
    def make_run(cls, label, notes, tasks, samples, passed, status="done",
                 env_cfg=None, progress=None, samples_written=None,
                 max_tokens=4096, fail_note=None, cut=0, errors=0):
        m = jobboard.new_bench_run(label, notes, tasks, samples, max_tokens)
        jid = m["id"]
        run = jobboard.bench_run_dir(jid)
        (run / "env.json").write_text(json.dumps(
            {"date": "2026-11-09 10:00", "hostname": "testhost", "model": "fake-model",
             "n_ctx": 262144, "basalt_commit": "aaa111", "config_sha256": "beef" * 16,
             "config": env_cfg or {"models.0.max-context": 262144, "parallel": 4}}),
            encoding="utf-8")
        total = len(tasks) * samples
        n_written = samples_written if samples_written is not None else total
        recs = []
        k = 0
        for s in range(1, samples + 1):
            for t in tasks:
                if k >= n_written:
                    break
                k += 1
                recs.append({"task": t, "sample": s, "seed": s,
                             "passed": passed, "total": 4, "score": passed / 4,
                             "completion_tokens": 50, "prompt_tokens": 100,
                             "decode_tps": 20.0, "prefill_tps": 500.0,
                             "ttft_s": 0.5, "total_s": 120.0, "finish": "stop",
                             "error": None, "tail": "", "t": time.time()})
        # cut: the last few answers ran into the token limit (still real scores)
        # errors: a few lines from an old version that scored a failed request 0
        for i, rec in enumerate(recs):
            if i < errors:
                rec.update(error="HTTPError: HTTP Error 503: Service Unavailable",
                           passed=0, score=0.0)
            elif len(recs) - i <= cut:
                rec["finish"] = "length"
        (run / "samples.jsonl").write_text(
            "".join(json.dumps(x) + "\n" for x in recs), encoding="utf-8")
        done = progress[0] if progress else len(recs)
        (run / "progress.json").write_text(json.dumps(
            {"done": done, "total": progress[1] if progress else total,
             "current": f"{tasks[-1]} sample {(done // len(tasks)) + 1}", "eta_s": 60}),
            encoding="utf-8")
        m = jobboard.load_meta(jid)
        m["status"] = status
        if status in ("done", "stopped", "interrupted", "failed"):
            m.update(started=time.time() - 900, finished=time.time(), exit=0)
        elif status == "running":
            m["started"] = time.time() - 300
        if fail_note:
            m["fail_note"] = fail_note
        jobboard.save_meta(m)
        return jid

    def get(self, path, cookie=True):
        code, _, body = fetch(self.port, jobboard.TOKEN, path, cookie=cookie)
        return code, body

    # ---------------------------------------------------------------- guard

    def test_every_bench_route_needs_the_cookie(self):
        self.assertTrue(jobboard.READONLY)
        paths = ["/bench", "/api/bench/rows", f"/bench/run/{self.done_a}",
                 f"/bench/compare?a={self.done_a}&b={self.done_b}",
                 f"/api/bench/live/{self.running}"]
        for p in paths:
            code, _ = self.get(p, cookie=False)
            self.assertEqual(code, 403, p)
            code, _ = self.get(p)
            self.assertEqual(code, 200, p)

    def test_posts_refused_in_readonly_mode(self):
        code, _, body = fetch(self.port, jobboard.TOKEN, "/bench/new",
                              data={"label": "x", "tasks": ["h2_cron"],
                                    "samples": "3", "max_tokens": "4096"})
        self.assertEqual(code, 403)
        self.assertIn("read-only", body.lower())

    # ---------------------------------------------------------------- /bench page

    def test_bench_page_form_and_history(self):
        code, h = self.get("/bench")
        self.assertEqual(code, 200)
        for needle in ("New bench run", 'action="/bench/new"',
                       "Hard tasks (recommended: these separate changes)",
                       "Main suite (nearly all pass", "Agent task (slow",
                       'placeholder="before: 256K ctx"',
                       "6-8 needed for a fair comparison",
                       '<option value="6" selected', 'value="32768"',
                       "about 30 answers",          # 5 hard tasks x 6 samples default
                       'name="a"', 'name="b"',
                       ):
            self.assertIn(needle, h)
        self.assertIn("before: slow ctx", h)           # history rows
        self.assertIn(f'href="/bench/run/{self.done_a}"', h)
        self.assertIn("fake-model ctx 262144", h)
        self.assertIn(f'<form method="post" action="/bench/resume/{self.stopped}"',
                      h)                               # Resume for stopped runs
        self.assertNotIn(f'<form method="post" action="/bench/resume/{self.done_a}"',
                         h)                            # not for finished ones
        self.assertIn(f'<form method="post" action="/bench/delete/{self.done_a}"', h)
        self.assertIn("Move this run to the trash?", h)

    def test_bench_page_live_card(self):
        code, h = self.get("/bench")
        self.assertIn('id="livecard"', h)
        # the bug: with one RUNNING run and one queued, the card showed the
        # queued one. It must show the running one; the rest count on one line.
        self.assertIn(f'data-live="{self.running}"', h)
        self.assertIn("+1 more bench runs queued", h)
        self.assertIn("queued run", h)                 # still in the history table
        self.assertIn("waiting in the queue", h)       # JS text for a QUEUED live run
        self.assertIn('id="runrows"', h)

    def _only_bench_runs(self, **kw):
        """Point JOBS/BENCH_RUNS at an empty pair of dirs and fill it with the
        given runs; returns (ids, restore-call)."""
        saved = (jobboard.JOBS, jobboard.BENCH_RUNS)
        d = Path(tempfile.mkdtemp(prefix="jbq-"))
        jobboard.JOBS = d / "jobs"; jobboard.JOBS.mkdir()
        jobboard.BENCH_RUNS = d / "runs"; jobboard.BENCH_RUNS.mkdir()
        ids = []
        for n, (label, st) in enumerate(kw.get("runs", [])):
            if n:
                time.sleep(1.05)   # run ids carry the second; keep the order clear
            ids.append(self.make_run(label, "", ["h2_cron"], 1, 0,
                                     status=st, progress=(0, 1), samples_written=0))

        def restore():
            jobboard.JOBS, jobboard.BENCH_RUNS = saved
        return ids, restore

    def test_live_card_is_the_oldest_queued_when_nothing_runs(self):
        ids, restore = None, None
        try:
            ids, restore = self._only_bench_runs(
                runs=[("first in line", "queued"), ("joined later", "queued")])
            q1, q2 = ids
            code, h = self.get("/bench")
            self.assertIn(f'data-live="{q1}"', h)       # oldest queued = next to start
            self.assertNotIn(f'data-live="{q2}"', h)
            self.assertIn("+1 more bench runs queued", h)
            self.assertNotIn("+2 more", h)
        finally:
            restore()

    def test_no_live_card_and_no_more_line_when_the_queue_is_empty(self):
        ids, restore = None, None
        try:
            ids, restore = self._only_bench_runs(runs=[("only run", "queued")])
            code, h = self.get("/bench")
            self.assertIn(f'data-live="{ids[0]}"', h)
            self.assertNotIn("more bench runs queued", h)
            code, h = self.get("/bench/run/" + ids[0])  # detail page: no +N line
            self.assertIn('id="livecard"', h)
            self.assertNotIn("more bench runs queued", h)
        finally:
            restore()

    def test_compare_button_exists_and_calls_goCompare(self):
        """The bug the review caught: the Before/After radios and goCompare() were
        both there, but nothing ever called it - the compare page had no button.
        The button must also be switched on only by a real pair of picks."""
        code, h = self.get("/bench")
        self.assertEqual(code, 200)
        i = h.index('id="cmpbtn"')
        tag = h[h.rindex("<", 0, i):h.index(">", i) + 1]
        self.assertIn('onclick="window.goCompare()"', tag)   # the wiring itself
        # goCompare lives inside the history script's IIFE now (shared global
        # scope was the live bug), so it must be exposed on window explicitly
        self.assertIn("window.goCompare=goCompare", h)
        self.assertIn("disabled", tag)                  # off until a pair is picked
        self.assertIn("Compare selected runs", h)
        self.assertIn('id="cmphint"', h)                # says what is missing
        self.assertIn("document.addEventListener('change',cmpState)", h)
        self.assertIn("cmpState();", h)                 # and runs on first paint

    def test_every_js_function_in_a_page_script_is_referenced(self):
        """Browser-free version of "does this button do anything": a function no
        one references is dead code, which is exactly how the missing Compare
        button stayed hidden. Every function a page defines must be named at
        least twice in that page (its definition plus a call or an onclick)."""
        # the pages that DO have page scripts (a finished run page has none)
        paths = ("/bench", f"/bench/run/{self.running}",
                 f"/bench/compare?a={self.done_a}&b={self.done_b}")
        for path in paths:
            code, html = self.get(path)
            self.assertEqual(code, 200, path)
            names = sorted(set(self.FUNC_RE.findall("".join(self.SCRIPT_RE.findall(html)))))
            self.assertTrue(names, f"{path}: no functions found at all")
            for name in names:
                used = len(re.findall(rf"\b{re.escape(name)}\b", html))
                self.assertGreaterEqual(
                    used, 2,
                    f"{path}: function {name}() is defined but nothing references it")

    def test_api_bench_rows_json(self):
        code, body = self.get("/api/bench/rows")
        self.assertEqual(code, 200)
        self.assertIn("before: slow ctx", body)        # rows_body HTML fragment
        self.assertIn(self.done_a, body)

    def test_api_bench_live(self):
        code, body = self.get(f"/api/bench/live/{self.running}")
        d = json.loads(body)
        self.assertEqual(d["state"], "WORKING")
        self.assertEqual((d["done"], d["total"]), (4, 6))
        self.assertEqual(d["current"], "p1_expr sample 3")
        self.assertIn("p1_expr", [s["task"] for s in d["scores"]])
        code, _ = self.get("/api/bench/live/not-a-run")
        self.assertEqual(code, 404)

    # ---------------------------------------------------------------- rows + pills

    def test_home_row_for_a_bench_job(self):
        code, h = self.get("/")
        self.assertIn(f'href="/bench/run/{self.done_a}"', h)
        self.assertIn("<td>bench</td>", h)             # mode column says bench
        self.assertIn("<td>-</td>", h)                 # folder shows as -
        self.assertIn("4/6 samples", h)                # pill_extra on the running run
        self.assertIn("bench", jobboard.job_rows())

    def test_status_for_shows_progress(self):
        code, body = self.get(f"/api/status/{self.running}")
        d = json.loads(body)
        self.assertEqual(d["state"], "WORKING")
        self.assertEqual(d["label"], "Running")
        self.assertEqual(d["pill_extra"], "4/6 samples")
        self.assertEqual(d["current_action"], "p1_expr sample 3")
        code, body = self.get(f"/api/status/{self.done_b}")
        d = json.loads(body)
        self.assertEqual(d["state"], "FINISHED")
        self.assertIn("overall", d["result_summary"])

    def test_job_page_redirects_to_bench_page(self):
        code, hdrs, _ = fetch(self.port, jobboard.TOKEN, f"/job/{self.done_a}")
        self.assertEqual(code, 303)
        self.assertEqual(hdrs.get("Location"), f"/bench/run/{self.done_a}")

    def test_bundle_refused_for_bench(self):
        code, body = self.get(f"/api/bundle/{self.done_a}")
        self.assertEqual(code, 404)
        self.assertIn("no review bundle", body)

    # ---------------------------------------------------------------- run detail

    def test_run_page(self):
        code, h = self.get(f"/bench/run/{self.done_b}")
        self.assertEqual(code, 200)
        self.assertIn("after: faster ctx", h)
        self.assertIn("raised max-context", h)         # notes
        self.assertIn("fake-model", h)
        self.assertIn("aaa111", h)                     # Basalt commit
        self.assertIn("Overall 100.0", h)
        self.assertIn("h2_cron", h)
        self.assertIn("models.0.max-context", h)       # config snapshot line
        self.assertIn(f'action="/bench/delete/{self.done_b}"', h)
        self.assertNotIn("Resume", h)                  # finished runs get no Resume

    def test_run_page_counts_cut_and_failed_answers(self):
        """Answers cut off by max_tokens are scores and get mentioned; a sample
        whose request failed is not a score and is reported as ignored."""
        code, h = self.get(f"/bench/run/{self.truncated}")
        self.assertEqual(code, 200)
        self.assertIn("<b>3</b> answers hit the token limit", h)   # cut answers: kept, counted
        self.assertIn("<b>1</b> failed request ignored", h)        # failed request: not a score
        self.assertIn("a stopped server is not a 0", h)
        self.assertNotIn("<b>4</b> answers hit", h)                # not miscounted

    def test_failed_run_says_why_and_can_be_resumed(self):
        """Exit 3 (the server stopped answering) is a pause, not the end: the run
        stays resumable so the missing sample is never scored 0."""
        code, h = self.get(f"/bench/run/{self.failed}")
        self.assertEqual(code, 200)
        self.assertIn("Basalt stopped answering - press Resume when it is back", h)
        self.assertIn(f'action="/bench/resume/{self.failed}"', h)
        code, h = self.get("/bench")
        self.assertIn(f'<form method="post" action="/bench/resume/{self.failed}"', h)
        by = {r["id"]: r for r in jobboard.bench_run_rows()}
        self.assertTrue(by[self.failed]["can_resume"])
        self.assertFalse(by[self.done_a]["can_resume"])    # finished stays finished
        # resume_bench takes a failed run back to the queue and clears the note
        self.assertIsNotNone(jobboard.resume_bench(self.failed))
        m = jobboard.load_meta(self.failed)
        self.assertEqual(m["status"], "queued")
        self.assertNotIn("fail_note", m)
        self.assertIsNone(jobboard.resume_bench(self.done_a))

    def test_missing_run_is_404(self):
        for bad in ("29990101-000000-zzz9", "not-an-id", "20261110-081500-ffff"):
            code, _ = self.get(f"/bench/run/{bad}")
            self.assertEqual(code, 404, bad)

    def test_run_dir_id_is_validated(self):
        self.assertIsNone(jobboard.bench_run_dir("../evil"))
        self.assertIsNone(jobboard.bench_run_dir(""))
        self.assertIsNone(jobboard.bench_run_dir("2026-11-10-x"))
        self.assertEqual(jobboard.bench_run_dir("20261110-081500-ab12"),
                         jobboard.BENCH_RUNS / "20261110-081500-ab12")

    # ---------------------------------------------------------------- compare

    def test_compare_page_verdict(self):
        code, h = self.get(f"/bench/compare?a={self.done_a}&b={self.done_b}")
        self.assertEqual(code, 200)
        self.assertIn("After is better", h)
        self.assertIn("+75.0 points (95% range +75.0 to +75.0)", h)
        self.assertIn("95% range", h)
        self.assertIn("bigger than the run-to-run noise", h)   # plain-words sentence
        self.assertIn("h2_cron", h)
        self.assertIn("Decode speed (t/s)", h)
        self.assertIn("models.0.max-context", h)       # what changed (env diff)
        self.assertNotIn("Nothing detected", h)
        self.assertIn("Copy summary", h)

    def test_compare_page_shows_the_chance_and_the_run_settings(self):
        # the review's example: only max_tokens changed, and env.json cannot see
        # that, so the settings from spec.json have to be listed too
        code, h = self.get(f"/bench/compare?a={self.done_a}&b={self.wider}")
        self.assertEqual(code, 200)
        self.assertIn("Model and config", h)
        self.assertIn("Run settings", h)
        self.assertIn("max_tokens", h)
        self.assertIn("4096", h)
        self.assertIn("32768", h)
        self.assertNotIn("Nothing detected", h)      # something really did change

        # only the model config differs: the settings line says the rest matched
        code, h = self.get(f"/bench/compare?a={self.done_a}&b={self.done_b}")
        self.assertEqual(code, 200)
        self.assertIn("Same tasks, same number of samples, same token limit.", h)
        self.assertNotIn("Nothing detected", h)
        # the chance (permutation p) in plain words, in the banner and per task
        self.assertIn("Chance of a gap this big if nothing really changed:", h)
        self.assertIn("<th>Chance</th>", h)
        self.assertIn("With several tasks, one can differ by chance", h)
        # a run compared with itself: now "nothing detected" is the honest answer
        code, h = self.get(f"/bench/compare?a={self.done_a}&b={self.done_a}")
        self.assertEqual(code, 200)
        self.assertIn("Nothing detected in the model, config or run settings", h)

    def test_compare_page_bad_params(self):
        for q in ("", "?a=zz&b=zz", f"?a={self.done_a}"):
            code, _ = self.get("/bench/compare" + q)
            self.assertEqual(code, 404, q)

    # ---------------------------------------------------------------- page scripts

    SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)
    FUNC_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")

    def test_bench_page_scripts_parse_with_node(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed: cannot syntax-check the page scripts")
        paths = ("/bench", f"/bench/run/{self.running}",     # the live card has a script
                 f"/bench/compare?a={self.done_a}&b={self.done_b}")
        for path in paths:
            code, html = self.get(path)
            self.assertEqual(code, 200, path)
            bodies = self.SCRIPT_RE.findall(html)
            self.assertTrue(bodies, f"no <script> block found in {path}")
            for n, body in enumerate(bodies):
                p = os.path.join(tempfile.mkdtemp(prefix="jscheck-"), f"script{n}.js")
                with open(p, "w") as f:
                    f.write(body)
                r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0,
                                 f"{path}: <script> #{n} failed node --check:\n{r.stderr}")

    def test_all_scripts_of_a_page_parse_together(self):
        """Classic <script> blocks share ONE global scope: one block declaring
        'let live' and another declaring 'function live()' compile apart but the
        second one dies at runtime with "Identifier 'live' has already been
        declared" (found on the live board: no 5 s refresh, dead Compare button).
        So ALL <script> blocks of a page, concatenated IN ORDER, must parse as
        one file - for every state the page can be in."""
        if not shutil.which("node"):
            self.skipTest("node is not installed: cannot syntax-check the page scripts")
        checks = ["/"]                                   # home page
        ids, restore = self._only_bench_runs(runs=[])
        try:
            checks.append("/bench")                      # state 1: no bench runs
        finally:
            restore()
        ids, restore = self._only_bench_runs(runs=[("just queued", "queued")])
        try:
            checks.append("/bench")                      # state 2: one queued
        finally:
            restore()
        # state 3: one running + one queued (the class fixture) - the state that
        # crashed on the live board, plus the run detail page
        checks += ["/bench", f"/bench/run/{self.running}",
                   f"/bench/compare?a={self.done_a}&b={self.done_b}"]
        for path in checks:
            code, html = self.get(path)
            self.assertEqual(code, 200, path)
            bodies = self.SCRIPT_RE.findall(html)
            self.assertTrue(bodies, f"no <script> block found in {path}")
            p = os.path.join(tempfile.mkdtemp(prefix="jscheck-"), "all-in-order.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(bodies))
            r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0,
                             f"{path}: all {len(bodies)} <script> blocks joined in order "
                             f"failed node --check:\n{r.stderr}")


# ------------------------------------------------------------------ saved review.md

class TestSavedReviewMd(unittest.TestCase):
    """The worker builds review.md when an agent job finishes; Copy for Claude
    and Download serve that file when it is NEWER than the job's finish time."""

    def setUp(self):
        self.saved = jobboard.JOBS
        jobboard.JOBS = Path(tempfile.mkdtemp(prefix="jb-md-"))

    def tearDown(self):
        jobboard.JOBS = self.saved

    def test_fresh_saved_bundle_is_used_and_stale_one_is_not(self):
        jid = "20261110-120000-ab12"
        jobboard.job_dir(jid).mkdir(parents=True)
        (jobboard.job_dir(jid) / "review.md").write_text("SAVED-1", encoding="utf-8")
        m = {"id": jid, "kind": "agent", "finished": time.time() - 10}
        self.assertEqual(jobboard.saved_review_md(m), "SAVED-1")
        m2 = {"id": jid, "kind": "agent", "finished": time.time() + 3600}
        self.assertIsNone(jobboard.saved_review_md(m2))   # older than the finish: rebuild

    def test_missing_file_and_bench_rows_are_no_bundle(self):
        self.assertIsNone(jobboard.saved_review_md({"id": "nope", "finished": 1}))
        self.assertIsNone(jobboard.saved_review_md(None))

    def test_build_skips_bench_runs(self):
        jid = "20261110-120000-cd34"
        jobboard.job_dir(jid).mkdir(parents=True)
        jobboard.build_review_md({"id": jid, "kind": "bench", "status": "done"})
        self.assertFalse((jobboard.job_dir(jid) / "review.md").exists())


# ------------------------------------------------------------------ real board, e2e

class EndToEndBoard(unittest.TestCase):
    """A real board process (worker running) + fake model server + bench-ref."""

    @classmethod
    def setUpClass(cls):
        cls.qf = None
        cls.ff = None
        cls.srv = None
        cls.proc = None
        cls.tmp = Path(tempfile.mkdtemp(prefix="jb-e2e-"))
        home = cls.tmp / "home"
        (home / "agent-sandbox").mkdir(parents=True)
        agent = home / "agent-sandbox" / "agent"
        agent.write_text('#!/bin/sh\nsleep 12\n'
                         'echo \'{"type":"result","is_error":false,"result":"done"}\'\n')
        agent.chmod(0o755)
        for sub in ("jobs", "work", "runs"):
            (cls.tmp / sub).mkdir()
        cfg = cls.tmp / "basalt_config.json"
        cfg.write_text(json.dumps({"models": [{"max-context": 262144}], "parallel": 4}),
                       encoding="utf-8")
        cls.qf = cls.tmp / "quality"
        cls.qf.write_text("good")
        # chat requests can be switched off from here (GET /models keeps working),
        # which is how test_07 makes the server "stop answering" mid-run
        cls.ff = cls.tmp / "chatfails"
        cls.ff.write_text("off")
        cls.srv = FakeServer(bench_dir=BENCH_REF, quality_file=str(cls.qf),
                             fail_file=str(cls.ff))
        cls.url = cls.srv.start()
        cls.port = free_port()
        env = dict(os.environ,
                   HOME=str(home), JOBBOARD_JOBS=str(cls.tmp / "jobs"),
                   JOBBOARD_WORK=str(cls.tmp / "work"), BENCH_RUNS=str(cls.tmp / "runs"),
                   BENCH_DIR=BENCH_REF, BENCH_URL=cls.url, BASALT_CONFIG=str(cfg),
                   BASALT_DIR=str(cls.tmp / "no-basalt-git"), JOBBOARD_PORT=str(cls.port),
                   BENCH_RETRY_WAITS="1")     # real policy waits 30/60/120 s
        env.pop("JOBBOARD_READONLY", None)
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "jobboard.py")], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True)
        cls.token = None
        deadline = time.time() + 15
        while time.time() < deadline:
            line = cls.proc.stdout.readline()
            if not line:
                break
            m = re.search(r"\?t=(\S+)", line)
            if m:
                cls.token = m.group(1)
                break
        if not cls.token:
            cls.tearDownClass()
            cls.fail("board subprocess did not print its token line")

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=10)
        if cls.srv:
            cls.srv.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # helpers ---------------------------------------------------------------

    def get(self, path):
        code, _, body = fetch(self.port, self.token, path)
        return code, body

    def meta(self, jid):
        try:
            return json.loads((self.tmp / "jobs" / jid / "meta.json").read_text())
        except (OSError, ValueError):
            return None

    def samples(self, jid):
        try:
            txt = (self.tmp / "runs" / jid / "samples.jsonl").read_text()
        except OSError:
            return []
        return [json.loads(x) for x in txt.splitlines() if x.strip()]

    def wait_status(self, jid, wanted, timeout=400):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            m = self.meta(jid)
            last = m and m.get("status")
            if last == wanted:
                return m
            time.sleep(1)
        self.fail(f"job {jid} never reached {wanted} (last status: {last})")

    def post_new_bench(self, label, tasks, samples):
        code, hdrs, body = fetch(self.port, self.token, "/bench/new",
                                 data={"label": label, "tasks": tasks,
                                       "samples": str(samples), "max_tokens": "2048"})
        self.assertEqual(code, 303, body)
        return hdrs.get("Location").rsplit("/", 1)[-1]

    # tests ------------------------------------------------------------------

    def test_01_form_validation_and_origin(self):
        base = {"tasks": ["h2_cron"], "samples": "1", "max_tokens": "2048"}
        code, _, body = fetch(self.port, self.token, "/bench/new",
                              data=base | {"label": "bad origin"},
                              origin="http://evil.example")
        self.assertEqual(code, 403, body)              # Origin check
        for bad in ({"label": ""}, {"label": "x", "tasks": ["not_a_task"]},
                    {"label": "x", "samples": "99"}, {"label": "x", "max_tokens": "9"},
                    {"label": "x", "samples": "abc"}):
            data = dict(base)
            data.update(bad)
            code, _, body = fetch(self.port, self.token, "/bench/new", data=data)
            self.assertEqual(code, 400, bad)
            self.assertIn("Pick a label", body)
        self.assertEqual([d.name for d in (self.tmp / "jobs").iterdir()], [])

    def test_02_bench_queues_behind_the_agent_job(self):
        code, hdrs, _ = fetch(self.port, self.token, "/new",
                              data={"mode": "local-claude", "newfolder": "ebench",
                                    "job": "sleep job"})
        self.assertEqual(code, 303)
        agent_id = hdrs.get("Location").rsplit("/", 1)[-1]
        self.wait_status(agent_id, "running", timeout=30)
        bench_id = self.post_new_bench("queue order", ["h2_cron"], 1)
        m = self.meta(bench_id)
        self.assertEqual(m["status"], "queued")        # worker is busy: it must wait
        both = False
        deadline = time.time() + 300
        while time.time() < deadline:
            am, bm = self.meta(agent_id), self.meta(bench_id)
            if am["status"] == "running" and bm["status"] == "queued":
                both = True                            # observed: agent runs, bench waits
            self.assertFalse(am["status"] == "running" and bm["status"] == "running",
                             "bench ran at the same time as the agent job")
            if bm["status"] in ("done", "failed"):
                break
            time.sleep(1)
        self.assertTrue(both, "never saw the bench queued while the agent ran")
        self.assertEqual(self.meta(bench_id)["status"], "done")
        recs = self.samples(bench_id)
        self.assertEqual([(r["task"], r["sample"]) for r in recs], [("h2_cron", 1)])

    def test_03_worker_reads_settings_only_from_the_spec(self):
        # busy the worker with an agent job so the bench run sits in the queue
        code, hdrs, _ = fetch(self.port, self.token, "/new",
                              data={"mode": "local-claude", "newfolder": "ebench",
                                    "job": "sleep job 2"})
        agent_id = hdrs.get("Location").rsplit("/", 1)[-1]
        self.wait_status(agent_id, "running", timeout=30)
        bench_id = self.post_new_bench("spec wins", ["h2_cron"], 1)
        meta = self.meta(bench_id)
        meta.update(job="TAMPERED", url="http://127.0.0.1:1/v1",   # a dead server:
                    tasks=["h1_lisp", "h5_puzzle"], samples=7,     # would fail if used
                    max_tokens=99)
        (self.tmp / "jobs" / bench_id / "meta.json").write_text(json.dumps(meta))
        self.wait_status(bench_id, "done", timeout=400)
        recs = self.samples(bench_id)
        self.assertEqual({r["task"] for r in recs}, {"h2_cron"})   # spec's tasks, not meta's
        self.assertEqual(len(recs), 1)                             # spec's sample count
        spec = json.loads((self.tmp / "runs" / bench_id / "spec.json").read_text())
        self.assertEqual(spec["label"], "spec wins")               # spec untouched
        self.assertEqual(spec["url"], self.url)                    # meta's url ignored

    def test_04_stop_then_resume(self):
        bench_id = self.post_new_bench("stop resume", ["h2_cron", "h1_lisp"], 2)
        self.wait_status(bench_id, "running", timeout=30)
        deadline = time.time() + 120
        while time.time() < deadline and not self.samples(bench_id):
            time.sleep(1)
        self.assertTrue(self.samples(bench_id), "no sample saved before the stop")
        code, hdrs, _ = fetch(self.port, self.token, f"/stop/{bench_id}", data={})
        self.assertEqual(code, 303)
        self.assertEqual(hdrs.get("Location"), f"/bench/run/{bench_id}")
        self.wait_status(bench_id, "stopped", timeout=60)
        partial = self.samples(bench_id)
        self.assertLess(len(partial), 4)               # stopped mid-run
        env_before = (self.tmp / "runs" / bench_id / "env.json").read_text()
        # a stopped run shows Resume on the history page; use the same POST a click sends
        code, h = self.get("/bench")
        self.assertIn(f'<form method="post" action="/bench/resume/{bench_id}"', h)
        code, _, _ = fetch(self.port, self.token, f"/bench/resume/{bench_id}", data={})
        self.assertEqual(code, 303)
        # queued again (the worker may already have grabbed it: either is fine,
        # "stopped" would not be)
        self.assertIn(self.meta(bench_id)["status"], ("queued", "running"))
        self.wait_status(bench_id, "done", timeout=400)
        recs = self.samples(bench_id)
        pairs = [(r["task"], r["sample"]) for r in recs]
        self.assertEqual(sorted(pairs), sorted({("h2_cron", 1), ("h2_cron", 2),
                                                ("h1_lisp", 1), ("h1_lisp", 2)}))
        self.assertEqual((self.tmp / "runs" / bench_id / "env.json").read_text(),
                         env_before)                  # env snapshot written once

    def test_05_before_after_compare_page(self):
        self.qf.write_text("bad")
        before = self.post_new_bench("before: broken ctx", ["h2_cron", "p1_expr"], 3)
        EndToEndBoard._before_id = before
        self.wait_status(before, "done", timeout=600)
        self.qf.write_text("good")
        after = self.post_new_bench("after: fixed ctx", ["h2_cron", "p1_expr"], 3)
        self.wait_status(after, "done", timeout=600)
        for r in self.samples(before):
            self.assertEqual(r["passed"], 0)
        for r in self.samples(after):
            self.assertEqual(r["passed"], r["total"])
        code, h = self.get("/bench")
        self.assertEqual(code, 200)
        self.assertIn("before: broken ctx", h)
        self.assertIn("after: fixed ctx", h)
        self.assertIn(">6/6<", h)                      # samples column
        code, h = self.get(f"/bench/compare?a={before}&b={after}")
        self.assertEqual(code, 200)
        self.assertIn("After is better", h)
        self.assertIn("100.0 points", h)               # 0 -> 100 overall
        self.assertIn("95% range", h)
        self.assertIn("Nothing detected in the model, config or run settings", h)  # all identical
        self.assertIn("Copy summary", h)

    def test_06_delete_moves_to_trash(self):
        bid = self.get_before()
        code, _, body = fetch(self.port, self.token, f"/bench/delete/{bid}", data={})
        self.assertEqual(code, 303, body)
        self.assertFalse((self.tmp / "runs" / bid).exists())
        self.assertTrue((self.tmp / "runs" / ".trash" / bid).is_dir())
        self.assertTrue((self.tmp / "jobs" / ".trash" / bid).is_dir())
        self.assertFalse((self.tmp / "jobs" / bid).exists())
        code, _ = self.get(f"/bench/run/{bid}")
        self.assertEqual(code, 404)

    def test_07_server_stops_answering_pauses_the_run(self):
        """A failed request is not a score: the pair is retried, and if the chat
        endpoint stays shut the run pauses (benchrun exit 3) with NOTHING recorded
        for that pair, so Resume can still finish it honestly."""
        self.ff.write_text("all")                  # chat 503s, /models still answers
        try:
            bid = self.post_new_bench("server went quiet", ["p1_expr"], 2)
            m = self.wait_status(bid, "failed", timeout=180)
            self.assertEqual(m.get("fail_note"),
                             "Basalt stopped answering - press Resume when it is back")
            self.assertEqual(self.samples(bid), [])        # no invented 0-score lines
            self.assertEqual(self.meta(bid)["exit"], 3)
            code, h = self.get(f"/bench/run/{bid}")
            self.assertEqual(code, 200)
            self.assertIn("Basalt stopped answering - press Resume when it is back", h)
            self.assertIn(f'action="/bench/resume/{bid}"', h)
            code, h = self.get("/bench")
            self.assertIn(f'<form method="post" action="/bench/resume/{bid}"', h)
        finally:
            self.ff.write_text("off")
        code, _, body = fetch(self.port, self.token, f"/bench/resume/{bid}", data={})
        self.assertEqual(code, 303, body)
        self.wait_status(bid, "done", timeout=400)
        recs = self.samples(bid)
        self.assertEqual(sorted(r["sample"] for r in recs), [1, 2])
        for r in recs:
            self.assertIsNone(r.get("error"))              # real answers, retried pairs
            self.assertEqual(r["passed"], r["total"])
        code, h = self.get(f"/bench/run/{bid}")
        self.assertNotIn("failed request ignored", h)      # nothing had to be ignored

    # keep the before-run id between tests without relying on method order tricks
    _before_id = None

    def get_before(self):
        # test_05 stores it; fall back to finding the run by label
        if EndToEndBoard._before_id:
            return EndToEndBoard._before_id
        for d in (self.tmp / "jobs").iterdir():
            m = self.meta(d.name)
            if m and m.get("job") == "before: broken ctx":
                EndToEndBoard._before_id = d.name
                return d.name
        self.fail("before run not found")


class RunnerReaper(unittest.TestCase):
    """A board restart can orphan the previous benchrun child; starting the run
    again must terminate it first (never two writers on samples.jsonl), and must
    never kill an unrelated pid that happens to reuse the number."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jb-reap-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spawn(self, *argv):
        return subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    def _wait_own_cmdline(self, p, needle, timeout=5.0):
        """Between fork and exec the child's /proc/<pid>/cmdline still holds
        the PARENT's command line (python -m unittest ...), so a reap run at
        that instant correctly sees no "benchrun.py" and skips - the test, not
        the code, is racing. Production runners have exec'd long ago; wait for
        the same situation (20 ms polls, 5 s ceiling)."""
        deadline = time.time() + timeout
        cmd = Path(f"/proc/{p.pid}/cmdline")
        while time.time() < deadline:
            try:
                if needle in cmd.read_bytes():
                    return
            except OSError:
                pass
            time.sleep(0.02)
        self.fail(f"/proc/{p.pid}/cmdline never showed {needle!r}")

    def test_terminates_a_live_benchrun_pid(self):
        script = self.tmp / "benchrun.py"
        script.write_text("import time\ntime.sleep(60)\n")
        p = self._spawn(sys.executable, str(script))
        try:
            self._wait_own_cmdline(p, b"benchrun.py")
            (self.tmp / "runner.pid").write_text(str(p.pid))
            jobboard.reap_old_runner(self.tmp)
            self.assertEqual(p.wait(timeout=10), -signal.SIGTERM)
        finally:
            if p.poll() is None:
                p.kill()

    def test_ignores_recycled_pid_that_is_not_benchrun(self):
        p = self._spawn("sleep", "60")
        try:
            self._wait_own_cmdline(p, b"sleep")   # test the real pid+cmdline path
            (self.tmp / "runner.pid").write_text(str(p.pid))
            jobboard.reap_old_runner(self.tmp)
            time.sleep(0.5)
            self.assertIsNone(p.poll())          # still alive: not ours to kill
        finally:
            p.kill()
            p.wait()

    def test_missing_or_garbage_pidfile_is_fine(self):
        jobboard.reap_old_runner(self.tmp)                 # no runner.pid
        (self.tmp / "runner.pid").write_text("not a pid")
        jobboard.reap_old_runner(self.tmp)
        (self.tmp / "runner.pid").write_text("999999999")  # pid that cannot exist
        jobboard.reap_old_runner(self.tmp)


if __name__ == "__main__":
    unittest.main()
