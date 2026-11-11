#!/usr/bin/env python3
"""Tests for benchrun: real grading against the fake streaming server.

The fake server answers each task prompt with the real reference solution (or a
broken one), so grading through bench-ref's own code runs for real here. The
real Basalt server is never contacted.

The fake server can also answer 503 to chat requests, which is how the retry /
pause paths are tested: fail_first=N fails the first N requests, fail_all keeps
failing them (see fakeserver.py). The real retry waits (30/60/120 s) are shrunk
with BENCH_RETRY_WAITS so nothing sleeps.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from fakeserver import FakeServer  # noqa: E402

BENCH_REF = str(ROOT / "bench-ref")


def read_jsonl(path):
    out = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    except OSError:
        pass
    return out


class BenchrunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FakeServer(bench_dir=BENCH_REF)
        cls.url = cls.srv.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="benchrun-"))
        self.cfg = self.tmp / "basalt_config.json"
        self.cfg.write_text(json.dumps(
            {"models": [{"max-context": 262144, "n-gpu-layers": 99}], "parallel": 4}),
            encoding="utf-8")

    def env(self, waits=None, **extra):
        e = dict(os.environ, BENCH_DIR=BENCH_REF, BASALT_CONFIG=str(self.cfg),
                 BASALT_DIR=str(self.tmp / "no-such-git-dir"))
        if waits is not None:          # real waits are 30/60/120 s; tests must not sleep
            e["BENCH_RETRY_WAITS"] = waits
        e.update(extra)
        return e

    def make_run(self, tasks, samples, url=None, **extra):
        run = self.tmp / "run"
        (run / "answers").mkdir(parents=True, exist_ok=True)
        spec = {"id": "20261110-080000-ab12", "label": "test run", "notes": "n",
                "tasks": tasks, "samples": samples, "max_tokens": 2048,
                "url": url or self.url}
        spec.update(extra)
        (run / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        return run

    def runit(self, run, timeout=180, env=None):
        return subprocess.run([sys.executable, str(ROOT / "benchrun.py"), "run", str(run)],
                              env=env or self.env(), capture_output=True, text=True,
                              timeout=timeout)

    # ---------------------------------------------------------------- happy path

    def test_round_robin_order_and_records(self):
        run = self.make_run(["h2_cron", "p1_expr"], 2)
        p = self.runit(run)
        self.assertEqual(p.returncode, 0, p.stderr)
        recs = read_jsonl(run / "samples.jsonl")
        # sample-major: every task's sample 1 before any sample 2
        self.assertEqual([(r["task"], r["sample"]) for r in recs],
                         [("h2_cron", 1), ("p1_expr", 1), ("h2_cron", 2), ("p1_expr", 2)])
        for r in recs:
            self.assertEqual(r["seed"], r["sample"])          # seed = sample number
            self.assertEqual(r["passed"], r["total"])          # reference answers pass
            self.assertEqual(r["score"], 1.0)
            self.assertTrue(r["total"] > 0)
            for k in ("completion_tokens", "prompt_tokens", "decode_tps", "prefill_tps",
                      "ttft_s", "total_s", "finish", "t"):
                self.assertIn(k, r)                            # every chat metric kept
            self.assertLessEqual(len(r["tail"]), 300)
        # answers saved
        self.assertEqual(sorted(os.listdir(run / "answers")),
                         ["h2_cron_s1.md", "h2_cron_s2.md", "p1_expr_s1.md", "p1_expr_s2.md"])
        prog = json.loads((run / "progress.json").read_text())
        self.assertEqual((prog["done"], prog["total"]), (4, 4))
        self.assertEqual(prog["current"], "p1_expr sample 2")
        self.assertEqual(prog["eta_s"], 0)

    def test_env_snapshot(self):
        run = self.make_run(["p1_expr"], 1)
        p = self.runit(run)
        self.assertEqual(p.returncode, 0, p.stderr)
        env = json.loads((run / "env.json").read_text())
        self.assertEqual(env["model"], "fake-model")
        self.assertEqual(env["n_ctx"], 262144)
        self.assertEqual(env["config_sha256"], hashlib.sha256(self.cfg.read_bytes()).hexdigest())
        self.assertEqual(env["config"]["models.0.max-context"], 262144)   # flattened copy
        self.assertEqual(env["config"]["parallel"], 4)
        self.assertEqual(env["basalt_commit"], "")            # BASALT_DIR does not exist
        self.assertTrue(env["hostname"])
        self.assertTrue(env["date"])

    # ---------------------------------------------------------------- resume

    def test_resume_skips_finished_pairs(self):
        run = self.make_run(["h2_cron", "p1_expr"], 1)
        self.assertEqual(self.runit(run).returncode, 0)
        first = read_jsonl(run / "samples.jsonl")
        self.assertEqual(len(first), 2)
        # same run dir, more samples: the finished pairs are not asked again
        (run / "spec.json").write_text(json.dumps(json.loads(
            (run / "spec.json").read_text()) | {"samples": 2}), encoding="utf-8")
        p = self.runit(run)
        self.assertEqual(p.returncode, 0, p.stderr)
        recs = read_jsonl(run / "samples.jsonl")
        self.assertEqual(len(recs), 4)
        pairs = [(r["task"], r["sample"]) for r in recs]
        self.assertEqual(len(set(pairs)), 4)                 # no duplicates
        self.assertIn(("h2_cron", 2), pairs)
        self.assertIn("already done", p.stdout)

    def test_resume_keeps_original_env_snapshot(self):
        run = self.make_run(["p1_expr"], 1)
        self.runit(run)
        before = (run / "env.json").read_text()
        self.runit(run)                                     # resume: nothing to do
        self.assertEqual((run / "env.json").read_text(), before)

    # ---------------------------------------------------------------- stop / down

    def test_sigterm_keeps_partial_data(self):
        # h2_cron first: its sample lands quickly; h1_lisp grading is slow, so the
        # SIGTERM arrives mid-run with some pairs saved and some not
        run = self.make_run(["h2_cron", "h1_lisp"], 6)
        p = subprocess.Popen([sys.executable, str(ROOT / "benchrun.py"), "run", str(run)],
                             env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        time.sleep(6)
        p.terminate()
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 143, err)
        recs = read_jsonl(run / "samples.jsonl")
        self.assertGreaterEqual(len(recs), 1)                 # what finished is saved
        self.assertLess(len(recs), 12)                        # and the run did not finish
        prog = json.loads((run / "progress.json").read_text())
        self.assertEqual(prog["done"], len(recs))
        self.assertTrue(prog["eta_s"] > 0)                    # ETA from mean secs/sample
        # resuming after the stop continues where it left off
        p2 = subprocess.Popen([sys.executable, str(ROOT / "benchrun.py"), "run", str(run)],
                              env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
        time.sleep(8)  # long enough for at least one more h1_lisp sample to finish
        p2.terminate()
        p2.communicate(timeout=60)
        recs2 = read_jsonl(run / "samples.jsonl")
        self.assertGreater(len(recs2), len(recs))

    def test_server_down_exit_2(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()
        run = self.make_run(["p1_expr"], 1, url=f"http://127.0.0.1:{dead_port}/v1")
        p = self.runit(run, timeout=60)
        self.assertEqual(p.returncode, 2)
        self.assertIn(str(dead_port), p.stderr)
        self.assertIn("did not answer", p.stderr)
        self.assertFalse((run / "samples.jsonl").exists())

    def test_unknown_task_exit_1(self):
        run = self.make_run(["not_a_task"], 1)
        p = self.runit(run)
        self.assertEqual(p.returncode, 1)
        self.assertIn("unknown task", p.stderr)

    def test_bad_answer_grades_zero(self):
        self.srv.quality = "bad"
        try:
            run = self.make_run(["h5_puzzle", "h2_cron"], 1)
            p = self.runit(run)
            self.assertEqual(p.returncode, 0, p.stderr)
            recs = read_jsonl(run / "samples.jsonl")
            self.assertTrue(all(r["passed"] == 0 for r in recs), recs)
            self.assertTrue(all(r["score"] == 0 for r in recs))
            self.assertTrue(all(r["total"] > 0 for r in recs))  # graded, not skipped
        finally:
            self.srv.quality = "good"

    # ------------------------------------------------- failed requests, retries
    # The real waits are 30/60/120 s; BENCH_RETRY_WAITS shrinks them so the
    # tests exercise the same code path without sleeping.

    def test_request_error_is_retried_then_scored(self):
        self.srv.fail_first = 2                      # two 503s, then it recovers
        self.srv.reset_chats()
        try:
            run = self.make_run(["p1_expr"], 1)
            p = self.runit(run, timeout=120, env=self.env(waits="0.05,0.1,0.2"))
        finally:
            self.srv.fail_first = 0
        self.assertEqual(p.returncode, 0, p.stderr)
        recs = read_jsonl(run / "samples.jsonl")
        self.assertEqual(len(recs), 1, recs)         # retried, recorded ONCE
        self.assertNotIn("error", recs[0])           # and it is a real score
        self.assertEqual(recs[0]["passed"], recs[0]["total"])
        self.assertGreater(recs[0]["total"], 0)
        self.assertIn("attempt 1 failed", p.stdout)   # the retries are visible
        self.assertIn("attempt 2 failed", p.stdout)
        self.assertIn("/models answered", p.stdout)  # the probe runs between tries
        self.assertIn("503", p.stdout)               # and says why it retried
        prog = json.loads((run / "progress.json").read_text())
        self.assertEqual((prog["done"], prog["total"]), (1, 1))

    def test_server_stops_answering_exit_3(self):
        self.srv.fail_all = True                     # chat dies, /models answers
        try:
            run = self.make_run(["p1_expr"], 2)
            p = self.runit(run, timeout=120, env=self.env(waits="0.05,0.1,0.2"))
        finally:
            self.srv.fail_all = False
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertFalse((run / "samples.jsonl").exists())   # never scored as 0
        self.assertFalse((run / "answers").exists() and list((run / "answers").iterdir()))
        prog = json.loads((run / "progress.json").read_text())
        self.assertEqual(prog["current"], "paused: the model server stopped answering")
        self.assertEqual(prog["done"], 0)
        self.assertIn("the run is paused", p.stderr)
        self.assertIn("press Resume", p.stderr)
        # resuming once the server answers again picks the same pair up
        run2 = run
        p2 = self.runit(run2, timeout=120, env=self.env(waits="0.05"))
        self.assertEqual(p2.returncode, 0, p2.stderr)
        recs = read_jsonl(run2 / "samples.jsonl")
        self.assertEqual(len(recs), 2, recs)

    def test_agent_task_without_qwen_aborts_at_once(self):
        # qwen IS installed here, so hide it with PATH. run_agent copies its
        # workspace under BENCH_DIR/results first, so point BENCH_DIR at a copy:
        # bench-ref must stay untouched.
        ref = self.tmp / "bench-copy"
        shutil.copytree(BENCH_REF, ref, ignore=shutil.ignore_patterns("__pycache__"))
        run = self.make_run(["agent"], 2)
        p = self.runit(run, timeout=180,
                       env=self.env(waits="0.05", PATH="/nonexistent", BENCH_DIR=str(ref)))
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("qwen", p.stderr)
        self.assertIn("stops now", p.stderr)
        self.assertFalse((run / "samples.jsonl").exists())   # no 0/total agent rows
        prog = json.loads((run / "progress.json").read_text())
        self.assertEqual(prog["done"], 0)


class BenchrunHelpers(unittest.TestCase):
    """benchrun's own small helpers, checked in-process (no server involved)."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT))
        import benchrun
        cls.br = benchrun

    def setUp(self):
        self.old = os.environ.pop("BENCH_RETRY_WAITS", None)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("BENCH_RETRY_WAITS", None)
        else:
            os.environ["BENCH_RETRY_WAITS"] = self.old

    def test_retry_waits_default_is_30_60_120(self):
        self.assertEqual(self.br.retry_waits(), [30.0, 60.0, 120.0])

    def test_retry_waits_from_env(self):
        os.environ["BENCH_RETRY_WAITS"] = "1,2"
        self.assertEqual(self.br.retry_waits(), [1.0, 2.0])
        os.environ["BENCH_RETRY_WAITS"] = "5, junk ,"
        self.assertEqual(self.br.retry_waits(), [5.0])         # junk skipped, no crash
        os.environ["BENCH_RETRY_WAITS"] = "-3"
        self.assertEqual(self.br.retry_waits(), [0.0])         # never sleeps backwards

    def test_fail_reason(self):
        self.assertIsNone(self.br.fail_reason({"finish": "stop", "total_s": 3}))
        self.assertIsNone(self.br.fail_reason({"finish": "length"}))  # cut off = a score
        self.assertEqual(self.br.fail_reason({"error": "HTTPError: HTTP Error 503"}),
                         "HTTPError: HTTP Error 503")
        self.assertEqual(self.br.fail_reason({"timeout": True}), "agent run timed out")

    def test_agent_timed_out_reads_the_log(self):
        tmp = Path(tempfile.mkdtemp(prefix="ws-"))
        (tmp / "_agent_log.txt").write_text("TIMEOUT\n", encoding="utf-8")
        self.assertTrue(self.br.agent_timed_out({"workspace": str(tmp)}))
        (tmp / "_agent_log.txt").write_text("done\n", encoding="utf-8")
        self.assertFalse(self.br.agent_timed_out({"workspace": str(tmp)}))
        self.assertFalse(self.br.agent_timed_out({}))                 # no workspace
        self.assertFalse(self.br.agent_timed_out({"workspace": str(tmp / "gone")}))


if __name__ == "__main__":
    unittest.main()
