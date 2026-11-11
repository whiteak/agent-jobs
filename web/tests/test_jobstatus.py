#!/usr/bin/env python3
"""Tests for jobstatus.py and the jobboard.py additions (standard library only).

Run:  cd /work/jobboard-ui && python3 -m unittest discover -s tests -v
"""
import builtins
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
SAMPLES = os.path.join(ROOT, "sample-jobs")
sys.path.insert(0, ROOT)

import jobstatus  # noqa: E402

# Import jobboard with isolated folders and read-only mode so importing it here
# is harmless (no worker, no writes outside the temp folders). Done before the
# import because the module reads its env at import time.
_s = socket.socket(); _s.bind(("127.0.0.1", 0)); TEST_PORT = _s.getsockname()[1]; _s.close()
_TMP_HOME = tempfile.mkdtemp(prefix="jbhome-")
_TMP_JOBS = tempfile.mkdtemp(prefix="jbjobs-")
_TMP_WORK = tempfile.mkdtemp(prefix="jbwork-")
os.environ.update({"HOME": _TMP_HOME, "JOBBOARD_JOBS": _TMP_JOBS, "JOBBOARD_WORK": _TMP_WORK,
                   "JOBBOARD_READONLY": "1", "JOBBOARD_PORT": str(TEST_PORT)})
import jobboard  # noqa: E402


def readj(path):
    with open(path) as f:
        return f.read()


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def meta(id="j1", status="running", folder="", exit=None, ago=60):
    t = time.time()
    return {"id": id, "mode": "local-claude", "folder": folder, "job": "Do the thing",
            "offline": False, "status": status, "created": t - ago, "started": t - ago,
            "finished": t - 5 if status in ("done", "failed", "interrupted", "stopped", "cancelled") else None,
            "exit": exit, "resume": None, "parent": None, "session": None}


def fresh_log(src):
    """Copy a fixture/sample log to a temp file with a fresh mtime."""
    d = tempfile.mkdtemp(prefix="jbfix-")
    p = os.path.join(d, "events.jsonl")
    shutil.copy(src, p)
    return p


def fresh_plan():
    """planproject fixture in a temp folder with a fresh PLAN.md mtime, so it
    falls inside a just-started job's own window (see PlanWindow tests)."""
    d = tempfile.mkdtemp(prefix="jbplan-")
    p = os.path.join(d, "PLAN.md")
    shutil.copy(os.path.join(FIX, "planproject", "PLAN.md"), p)
    os.utime(p, None)
    return d


class StatusBase(unittest.TestCase):
    def setUp(self):
        jobstatus.reset_cache()

    def tearDown(self):
        jobstatus.reset_cache()


# ------------------------------------------------------------------ state mapping

class StateMapping(StatusBase):
    def setUp(self):
        super().setUp()
        self.ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))

    def st(self, status, **kw):
        return jobstatus.job_status(meta(status=status, **kw), self.ev, "", time.time())["state"]

    def test_queued(self):
        self.assertEqual(self.st("queued"), "QUEUED")

    def test_running(self):
        self.assertEqual(self.st("running"), "WORKING")

    def test_stopping(self):
        self.assertEqual(self.st("stopping"), "WORKING")

    def test_done(self):
        self.assertEqual(self.st("done"), "FINISHED")

    def test_failed(self):
        self.assertEqual(self.st("failed"), "FAILED")

    def test_interrupted(self):
        self.assertEqual(self.st("interrupted"), "FAILED")

    def test_stopped(self):
        self.assertEqual(self.st("stopped"), "STOPPED")

    def test_cancelled(self):
        self.assertEqual(self.st("cancelled"), "STOPPED")

    def test_labels(self):
        s = jobstatus.job_status(meta(status="queued"), self.ev, "", time.time())
        self.assertEqual(s["label"], "Waiting in queue")
        self.assertEqual(s["current_action"], "Waiting in the queue")


# ------------------------------------------------------------------ stuck detection

class StuckDetection(StatusBase):
    def test_fresh_running_is_working(self):
        ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))
        s = jobstatus.job_status(meta(status="running"), ev, "", time.time())
        self.assertEqual(s["state"], "WORKING")
        self.assertLess(s["last_activity_ago"], 10)

    def test_aged_log_is_stuck(self):
        ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))
        old = time.time() - 400
        os.utime(ev, (old, old))
        s = jobstatus.job_status(meta(status="running"), ev, "", time.time())
        self.assertEqual(s["state"], "STUCK")
        self.assertGreater(s["last_activity_ago"], 180)

    def test_stopping_also_goes_stuck(self):
        ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))
        old = time.time() - 300
        os.utime(ev, (old, old))
        s = jobstatus.job_status(meta(status="stopping"), ev, "", time.time())
        self.assertEqual(s["state"], "STUCK")

    def test_done_never_stuck(self):
        ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))
        old = time.time() - 9000
        os.utime(ev, (old, old))
        s = jobstatus.job_status(meta(status="done"), ev, "", time.time())
        self.assertEqual(s["state"], "FINISHED")


# ------------------------------------------------------------------ current action

def action_of(name, inp):
    d = tempfile.mkdtemp(prefix="jbact-")
    ev = os.path.join(d, "events.jsonl")
    with open(ev, "w") as f:
        f.write(json.dumps({"type": "system", "subtype": "init", "session_id": "s"}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"content":
                [{"type": "tool_use", "id": "t", "name": name, "input": inp}]}}) + "\n")
    return jobstatus.job_status(meta(status="running"), ev, "", time.time())["current_action"]


class CurrentAction(StatusBase):
    def test_bash_tests(self):
        for cmd in ("cd /work/x && pytest tests/ -q", "./gradlew test", "npm test",
                    "ctest --output-on-failure", "python3 -m unittest discover -s tests -v",
                    "go test ./...", "make test"):
            self.assertEqual(action_of("Bash", {"command": cmd}), "Running tests", cmd)

    def test_bash_build(self):
        for cmd in ("./gradlew assembleDebug", "./gradlew build", "make -j4", "cmake -B build",
                    "npm run build"):
            self.assertEqual(action_of("Bash", {"command": cmd}), "Building", cmd)

    def test_bash_other(self):
        self.assertEqual(action_of("Bash", {"command": "ls -la /work/demo"}), "Running: ls -la /work/demo")

    def test_bash_truncates(self):
        a = action_of("Bash", {"command": "x" * 120})
        self.assertEqual(a, "Running: " + "x" * 60)

    def test_edit_tools(self):
        self.assertEqual(action_of("Edit", {"file_path": "/work/x/app/src/Main.kt"}), "Editing Main.kt")
        self.assertEqual(action_of("Write", {"file_path": "/work/x/PLAN.md"}), "Editing PLAN.md")
        self.assertEqual(action_of("MultiEdit", {"file_path": "/a/b/c.py"}), "Editing c.py")

    def test_read(self):
        self.assertEqual(action_of("Read", {"file_path": "/work/x/build.gradle"}), "Reading build.gradle")

    def test_search(self):
        self.assertEqual(action_of("Grep", {"pattern": "Foo"}), "Searching the code")
        self.assertEqual(action_of("Glob", {"pattern": "*.kt"}), "Searching the code")

    def test_web(self):
        self.assertEqual(action_of("WebFetch", {"url": "http://example.com"}), "Looking something up online")
        self.assertEqual(action_of("WebSearch", {"query": "q"}), "Looking something up online")

    def test_qwen_names(self):
        self.assertEqual(action_of("run_shell_command", {"command": "pytest -q"}), "Running tests")
        self.assertEqual(action_of("run_shell_command", {"command": "gradlew assembleRelease"}), "Building")
        self.assertEqual(action_of("run_shell_command", {"command": "echo hi"}), "Running: echo hi")
        self.assertEqual(action_of("edit", {"file_path": "/a/b/foo.py"}), "Editing foo.py")
        self.assertEqual(action_of("write_file", {"file_path": "/a/b/bar.py"}), "Editing bar.py")
        self.assertEqual(action_of("read_file", {"file_path": "/x/y.txt"}), "Reading y.txt")
        self.assertEqual(action_of("grep_search", {"pattern": "p"}), "Searching the code")
        self.assertEqual(action_of("glob", {"pattern": "*"}), "Searching the code")
        self.assertEqual(action_of("web_fetch", {"url": "u"}), "Looking something up online")

    def test_thinking(self):
        d = tempfile.mkdtemp(prefix="jbact-")
        ev = os.path.join(d, "events.jsonl")
        with open(ev, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content":
                    [{"type": "thinking", "thinking": "hmm"}]}}) + "\n")
        self.assertEqual(jobstatus.job_status(meta(), ev, "", time.time())["current_action"], "Thinking")

    def test_thinking_tokens(self):
        ev = os.path.join(FIX, "running", "events.jsonl")  # not used; make own
        d = tempfile.mkdtemp(prefix="jbact-")
        ev = os.path.join(d, "events.jsonl")
        with open(ev, "w") as f:
            f.write(json.dumps({"type": "system", "subtype": "thinking_tokens",
                                "estimated_tokens": 3, "session_id": "s"}) + "\n")
        self.assertEqual(jobstatus.job_status(meta(), ev, "", time.time())["current_action"], "Thinking")

    def test_text(self):
        d = tempfile.mkdtemp(prefix="jbact-")
        ev = os.path.join(d, "events.jsonl")
        with open(ev, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content":
                    [{"type": "text", "text": "Let me explain."}]}}) + "\n")
        self.assertEqual(jobstatus.job_status(meta(), ev, "", time.time())["current_action"], "Writing a reply")

    def test_real_sample_latest_event_wins(self):
        # The 432hs job's last events are a Bash tool call followed by thinking_tokens spam;
        # the true final answer event wins over the noise.
        ev = os.path.join(SAMPLES, "20261108-215755-eb6e", "events.jsonl")
        m = json.loads(readj(os.path.join(SAMPLES, "20261108-215755-eb6e", "meta.json")))
        s = jobstatus.job_status(m, ev, "", time.time())
        self.assertEqual(s["state"], "FINISHED")
        self.assertEqual(s["actions"], 36)
        self.assertEqual(s["turns"], 37)  # num_turns from the result line, not the tool count


# ------------------------------------------------------------------ steps

class Steps(StatusBase):
    def test_todo_log_latest_wins(self):
        ev = os.path.join(FIX, "todowrite", "events.jsonl")
        s = jobstatus.job_status(meta(status="running"), ev, os.path.join(FIX, "planproject"), time.time())
        self.assertEqual(s["steps_source"], "todo-log")
        self.assertEqual([t["text"] for t in s["steps"]],
                         ["Set up the project", "Write the code", "Run the tests"])
        # second TodoWrite call: first completed, second in progress
        self.assertEqual([t["state"] for t in s["steps"]], ["done", "active", "todo"])
        self.assertEqual((s["steps_done"], s["steps_total"]), (1, 3))

    def test_task_tools(self):
        d = tempfile.mkdtemp(prefix="jbtask-")
        ev = os.path.join(d, "events.jsonl")
        with open(ev, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content":
                    [{"type": "tool_use", "name": "TaskCreate", "input": {"subject": "One"}}]}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content":
                    [{"type": "tool_use", "name": "TaskCreate", "input": {"subject": "Two"}}]}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content":
                    [{"type": "tool_use", "name": "TaskUpdate", "input": {"taskId": "t1", "status": "completed"}}]}}) + "\n")
        s = jobstatus.job_status(meta(status="running"), ev, "", time.time())
        self.assertEqual(s["steps_source"], "todo-log")
        self.assertEqual([t["state"] for t in s["steps"]], ["done", "active"])  # first unticked = active

    def test_plan_md_fallback(self):
        ev = os.path.join(FIX, "running", "events.jsonl")
        s = jobstatus.job_status(meta(status="running"), ev, fresh_plan(), time.time())
        self.assertEqual(s["steps_source"], "PLAN.md")
        self.assertEqual([t["text"] for t in s["steps"]],
                         ["first step done", "second step open", "third step numbered done", "nested open step"])
        # while working, the first unticked step becomes active
        self.assertEqual([t["state"] for t in s["steps"]], ["done", "active", "done", "todo"])
        self.assertEqual((s["steps_done"], s["steps_total"]), (2, 4))

    def test_plan_done_when_finished_not_active(self):
        ev = os.path.join(FIX, "running", "events.jsonl")
        s = jobstatus.job_status(meta(status="stopped"), ev, fresh_plan(), time.time())
        self.assertEqual([t["state"] for t in s["steps"]], ["done", "todo", "done", "todo"])

    def test_agent_report_fallback(self):
        d = tempfile.mkdtemp(prefix="jbrep-")
        with open(os.path.join(d, "AGENT_REPORT.md"), "w") as f:
            f.write("# Report\n\n- [x] did the thing\n- [ ] still open\n")
        ev = os.path.join(FIX, "running", "events.jsonl")
        s = jobstatus.job_status(meta(status="running"), ev, d, time.time())
        self.assertEqual(s["steps_source"], "AGENT_REPORT.md")
        self.assertEqual(s["steps_total"], 2)

    def test_no_plan_fallback_text(self):
        ev = os.path.join(FIX, "running", "events.jsonl")
        d = tempfile.mkdtemp(prefix="jbnone-")
        s = jobstatus.job_status(meta(status="running"), ev, d, time.time())
        self.assertEqual(s["steps_total"], 0)
        self.assertEqual(s["fallback"], "No plan yet; 1 action so far")
        self.assertEqual(s["steps_source"], "")


# ------------------------------------------------------------------ plan file window

class PlanWindow(StatusBase):
    """A project folder keeps its PLAN.md / AGENT_REPORT.md between jobs; only
    files modified inside the job's own window (started-5 .. finished+120) may
    supply the checklist."""

    def proj(self, mtime, plan="- [x] a step\n- [ ] another\n"):
        d = tempfile.mkdtemp(prefix="jbwin-")
        p = os.path.join(d, "PLAN.md")
        with open(p, "w") as f:
            f.write(plan)
        os.utime(p, (mtime, mtime))
        return d

    def test_plan_older_than_job_start_ignored(self):
        ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))
        m = meta(status="running")  # started 60 s ago
        s = jobstatus.job_status(m, ev, self.proj(time.time() - 3600), time.time())
        self.assertEqual(s["steps_total"], 0)
        self.assertEqual(s["steps_source"], "")
        self.assertEqual(s["fallback"], "No plan yet; 1 action so far")

    def test_plan_modified_long_after_finish_ignored(self):
        ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))
        m = meta(status="done")  # finished 5 s ago -> window ends at now+115
        s = jobstatus.job_status(m, ev, self.proj(time.time() + 400), time.time())
        self.assertEqual(s["steps_total"], 0)
        self.assertEqual(s["steps_source"], "")

    def test_plan_inside_window_used(self):
        ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))
        m = meta(status="done", ago=600)  # started 10 min ago, finished 5 s ago
        s = jobstatus.job_status(m, ev, self.proj(time.time() - 300), time.time())
        self.assertEqual(s["steps_source"], "PLAN.md")
        self.assertEqual(s["steps_total"], 2)

    def test_running_job_fresh_plan_used(self):
        ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))
        m = meta(status="running")
        s = jobstatus.job_status(m, ev, self.proj(time.time() + 3600), time.time())
        self.assertEqual(s["steps_source"], "PLAN.md")  # running: no upper bound

    def test_queued_job_never_uses_files(self):
        ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))
        m = meta(status="queued")
        s = jobstatus.job_status(m, ev, self.proj(time.time()), time.time())
        self.assertEqual(s["steps_total"], 0)
        self.assertEqual(s["steps_source"], "")

    def test_source_only_when_checkboxes_found(self):
        # plan inside the window but with no checkboxes at all -> no steps_source
        ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))
        m = meta(status="running")
        s = jobstatus.job_status(m, ev, self.proj(time.time(), plan="just prose, no list\n"), time.time())
        self.assertEqual(s["steps_source"], "")
        self.assertEqual(s["steps_total"], 0)
        self.assertEqual(s["fallback"], "No plan yet; 1 action so far")

    def test_agent_report_window_respected(self):
        d = tempfile.mkdtemp(prefix="jbwin2-")
        p = os.path.join(d, "AGENT_REPORT.md")
        with open(p, "w") as f:
            f.write("- [x] old job\n")
        os.utime(p, (time.time() - 3600,) * 2)
        ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))
        s = jobstatus.job_status(meta(status="running"), ev, d, time.time())
        self.assertEqual(s["steps_source"], "")


# ------------------------------------------------------------------ results

class Results(StatusBase):
    def test_result_summary(self):
        ev = os.path.join(SAMPLES, "20261108-215755-eb6e", "events.jsonl")
        m = json.loads(readj(os.path.join(SAMPLES, "20261108-215755-eb6e", "meta.json")))
        s = jobstatus.job_status(m, ev, "", time.time())
        self.assertEqual(s["state"], "FINISHED")
        self.assertTrue(s["result_summary"])
        self.assertLessEqual(len(s["result_summary"]), 200)
        self.assertNotIn("\n", s["result_summary"])
        self.assertNotIn("`", s["result_summary"])  # markdown flattened
        self.assertTrue(s["elapsed"] > 300)

    def test_fail_reason_is_error_text(self):
        ev = os.path.join(FIX, "failed", "events.jsonl")
        s = jobstatus.job_status(meta(status="failed"), ev, "", time.time())
        self.assertEqual(s["state"], "FAILED")
        self.assertIn("cannot find symbol", s["fail_reason"])

    def test_fail_reason_interrupted(self):
        ev = os.path.join(FIX, "running", "events.jsonl")
        s = jobstatus.job_status(meta(status="interrupted"), ev, "", time.time())
        self.assertEqual(s["fail_reason"], "Interrupted: the job board restarted while it ran")

    def test_fail_reason_exit_code(self):
        ev = os.path.join(FIX, "running", "events.jsonl")
        s = jobstatus.job_status(meta(status="failed", exit=7), ev, "", time.time())
        self.assertEqual(s["fail_reason"], "Exit code 7")

    def test_plain_text(self):
        p = jobstatus.plain_text
        self.assertEqual(p("**Done** — `run_tests.sh` passed"), "Done — run_tests.sh passed")
        self.assertEqual(p("[the report](file:///x/AGENT_REPORT.md) is ready"), "the report is ready")
        self.assertEqual(p("# Summary\n\n- first\n* second\n1. third"), "Summary first second third")
        self.assertEqual(p("snake_case_name and __dunder__"), "snake_case_name and dunder")
        self.assertEqual(p("  a\n\n  b\t c  "), "a b c")
        self.assertEqual(p(None), "")

    def test_result_summary_markdown_stripped(self):
        d = tempfile.mkdtemp(prefix="jbmd-")
        ev = os.path.join(d, "events.jsonl")
        res = "**Fixed** the `assembleDebug` error; see [the report](file:///x/AGENT_REPORT.md).\n\n- rebuilt APK"
        with open(ev, "w") as f:
            f.write(json.dumps({"type": "result", "result": res, "is_error": False, "num_turns": 4}) + "\n")
        s = jobstatus.job_status(meta(status="done"), ev, "", time.time())
        self.assertEqual(s["result_summary"], "Fixed the assembleDebug error; see the report. rebuilt APK")

    def test_fail_reason_markdown_stripped(self):
        d = tempfile.mkdtemp(prefix="jbmd2-")
        ev = os.path.join(d, "events.jsonl")
        with open(ev, "w") as f:
            f.write(json.dumps({"type": "result", "result": "Build **failed**: `e: Main.kt` cannot find symbol",
                                "is_error": True, "num_turns": 2}) + "\n")
        s = jobstatus.job_status(meta(status="failed"), ev, "", time.time())
        self.assertEqual(s["fail_reason"], "Build failed: e: Main.kt cannot find symbol")

    def test_stopped_sample_thinking(self):
        ev = os.path.join(SAMPLES, "20261107-180847-ffe9", "events.jsonl")
        m = json.loads(readj(os.path.join(SAMPLES, "20261107-180847-ffe9", "meta.json")))
        s = jobstatus.job_status(m, ev, "", time.time())
        self.assertEqual(s["state"], "STOPPED")
        self.assertEqual(s["actions"], 0)
        self.assertEqual(s["fallback"], "No plan yet; 0 actions so far")


# ------------------------------------------------------------------ incremental parsing

class _ReadRecorder:
    """Patch-in for jobstatus.open that records seek positions and bytes read
    for one specific file."""
    def __init__(self, target):
        self.target = os.path.abspath(target)
        self.calls = []

    def __call__(self, file, *a, **k):
        f = builtins.open(file, *a, **k)
        if os.path.abspath(str(file)) != self.target:
            return f
        rec = {"seek": None, "bytes": 0}
        self.calls.append(rec)

        class W:
            def __enter__(self): return self
            def __exit__(self, *x): f.close(); return False
            def seek(self, n): rec["seek"] = n; return f.seek(n)
            def read(self, n=-1):
                d = f.read(n); rec["bytes"] += len(d); return d
        return W()


class Incremental(StatusBase):
    @staticmethod
    def patch_open(rec):
        old = getattr(jobstatus, "open", None)
        jobstatus.open = rec
        def undo():
            if old is None:
                del jobstatus.open
            else:
                jobstatus.open = old
        return undo

    def test_appends_do_not_reparse_from_zero(self):
        ev = fresh_log(os.path.join(FIX, "running", "events.jsonl"))
        size1 = os.path.getsize(ev)
        s1 = jobstatus.job_status(meta(status="running"), ev, "", time.time())
        self.assertEqual(s1["actions"], 1)
        self.assertEqual(s1["current_action"], "Running tests")

        # no change -> the events file is not opened at all
        rec = _ReadRecorder(ev)
        undo = self.patch_open(rec)
        try:
            s2 = jobstatus.job_status(meta(status="running"), ev, "", time.time())
        finally:
            undo()
        self.assertEqual(rec.calls, [])
        self.assertEqual(s2["actions"], 1)

        # append one line -> only the new bytes are read, from the old offset
        with open(ev, "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content":
                    [{"type": "tool_use", "id": "t9", "name": "Bash",
                      "input": {"command": "ls -la"}}]}}) + "\n")
        rec2 = _ReadRecorder(ev)
        undo = self.patch_open(rec2)
        try:
            s3 = jobstatus.job_status(meta(status="running"), ev, "", time.time())
        finally:
            undo()
        self.assertEqual(len(rec2.calls), 1)
        self.assertEqual(rec2.calls[0]["seek"], size1)          # not 0
        self.assertEqual(rec2.calls[0]["bytes"], os.path.getsize(ev) - size1)
        self.assertEqual(s3["actions"], 2)
        self.assertEqual(s3["current_action"], "Running: ls -la")


# ------------------------------------------------------------------ notify_finished

class Notify(unittest.TestCase):
    OK_SND = "/usr/share/sounds/freedesktop/stereo/complete.oga"
    BAD_SND = "/usr/share/sounds/freedesktop/stereo/dialog-error.oga"

    def mocks(self, available):
        """Patch subprocess.run + shutil.which (only `available` players exist)."""
        from unittest import mock
        self.which = mock.patch.object(
            jobboard.shutil, "which",
            side_effect=lambda c: f"/usr/bin/{c}" if c in available else None).start()
        self.run = mock.patch.object(jobboard.subprocess, "run").start()

    def tearDown(self):
        from unittest import mock
        mock.patch.stopall()

    def test_success_notification_and_sound(self):
        self.mocks({"paplay"})  # paplay-only box
        # a desktop shell already has these set; the test is about the fallback values
        from unittest import mock
        with mock.patch.dict(os.environ):
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
            os.environ.pop("XDG_RUNTIME_DIR", None)
            jobboard.notify_finished({"id": "x", "job": "Build the app and run tests"}, True)
        self.assertEqual(self.run.call_count, 2)
        ns, pa = self.run.call_args_list[0][0][0], self.run.call_args_list[1][0][0]
        self.assertEqual(ns[:2], ["notify-send", "Agent job finished"])
        self.assertIn("Build the app", ns[2])
        self.assertEqual(pa[0], "paplay")
        self.assertEqual(pa[1], self.OK_SND)
        uid = os.getuid()
        env = self.run.call_args_list[0][1]["env"]
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], f"unix:path=/run/user/{uid}/bus")
        self.assertEqual(env["XDG_RUNTIME_DIR"], f"/run/user/{uid}")

    def test_pw_play_preferred(self):
        self.mocks({"pw-play", "paplay", "canberra-gtk-play"})  # Jason's PipeWire desktop
        jobboard.notify_finished({"id": "x", "job": "y"}, True)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.run.call_args_list[1][0][0], ["pw-play", self.OK_SND])

    def test_paplay_fallback(self):
        self.mocks({"paplay", "canberra-gtk-play"})
        jobboard.notify_finished({"id": "x", "job": "y"}, True)
        self.assertEqual(self.run.call_args_list[1][0][0], ["paplay", self.OK_SND])

    def test_canberra_fallback(self):
        self.mocks({"canberra-gtk-play"})
        jobboard.notify_finished({"id": "x", "job": "y"}, False)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.run.call_args_list[1][0][0], ["canberra-gtk-play", "-f", self.BAD_SND])

    def test_no_player_no_sound_no_crash(self):
        self.mocks(set())
        jobboard.notify_finished({"id": "x", "job": "y"}, True)  # must not raise
        self.assertEqual(self.run.call_count, 1)  # notify-send only, nothing sound-related
        self.assertEqual(self.run.call_args_list[0][0][0][0], "notify-send")

    def test_failure_sound(self):
        self.mocks({"paplay"})
        jobboard.notify_finished({"id": "x", "job": "job text"}, False)
        ns, pa = self.run.call_args_list[0][0][0], self.run.call_args_list[1][0][0]
        self.assertEqual(ns[1], "Agent job failed")
        self.assertEqual(pa[1], self.BAD_SND)

    def test_survives_missing_commands(self):
        self.mocks({"paplay"})
        self.run.side_effect = FileNotFoundError("no notify-send")
        jobboard.notify_finished({"id": "x", "job": "y"}, True)  # must not raise

    def test_survives_other_errors(self):
        self.mocks({"pw-play"})
        self.run.side_effect = OSError("boom")
        jobboard.notify_finished({"id": "x", "job": "y"}, False)


# ------------------------------------------------------------------ read-only mode

class ReadOnlyMode(unittest.TestCase):
    def test_import_does_not_start_worker(self):
        env = dict(os.environ)  # already JOBBOARD_READONLY=1 + temp folders
        r = subprocess.run([sys.executable, "-c",
                            "import sys,threading;sys.path.insert(0,%r);import jobboard;"
                            "names=[t.name for t in threading.enumerate()];"
                            "sys.exit(0 if names==['MainThread'] else 1)" % ROOT],
                           env=env, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr.decode()[:400])

    def test_running_job_not_marked_interrupted(self):
        jobs = tempfile.mkdtemp(prefix="jbro-"); work = tempfile.mkdtemp(prefix="jbro-")
        home = tempfile.mkdtemp(prefix="jbro-")
        jd = os.path.join(jobs, "jx1"); os.makedirs(jd)
        with open(os.path.join(jd, "meta.json"), "w") as f:
            json.dump(meta(id="jx1", status="running"), f)
        port = free_port()
        env = dict(os.environ, HOME=home, JOBBOARD_JOBS=jobs, JOBBOARD_WORK=work,
                   JOBBOARD_READONLY="1", JOBBOARD_PORT=str(port))
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "jobboard.py")],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            first = p.stdout.readline()
            self.assertIn("Read-only mode", first)
            time.sleep(1.5)
            st = json.loads(readj(os.path.join(jd, "meta.json")))
            self.assertEqual(st["status"], "running")  # a worker would have made it "interrupted"
        finally:
            p.terminate()
            try: p.wait(timeout=5)
            except subprocess.TimeoutExpired: p.kill()


# ------------------------------------------------------------------ server smoke test

class ServerSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = os.path.join(SAMPLES, "20261107-180819-71a7")
        dst = os.path.join(_TMP_JOBS, "20261107-180819-71a7")
        if not os.path.isdir(dst):
            shutil.copytree(src, dst)
        # a project folder with a PLAN.md so the checklist renders
        pj = os.path.join(_TMP_WORK, "webtest")
        os.makedirs(pj, exist_ok=True)
        plan = os.path.join(pj, "PLAN.md")
        with open(plan, "w") as f:
            f.write("- [x] write hello.sh\n- [ ] run it\n")
        # the sample job ended days ago: put the plan's mtime inside its own
        # window, or jobstatus.plan_in_window would (correctly) ignore the file
        mm = json.loads(readj(os.path.join(dst, "meta.json")))
        t = (mm.get("finished") or mm.get("created") or time.time()) - 5
        os.utime(plan, (t, t))
        # a synthetic "running" job so the live-update half of the job-page
        # script is exercised too (stop form, WORKING state)
        cls.runid = "29990101-000000-run1"
        rd = os.path.join(_TMP_JOBS, cls.runid)
        os.makedirs(rd, exist_ok=True)
        with open(os.path.join(rd, "meta.json"), "w") as f:
            json.dump(meta(id=cls.runid, status="running", folder="webtest"), f)
        with open(os.path.join(rd, "events.jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content":
                    [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}}) + "\n")
        cls.jid = "20261107-180819-71a7"
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", TEST_PORT), jobboard.H)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thr.join(timeout=5)

    def fetch(self, path, cookie=True, method="GET", data=None):
        req = urllib.request.Request(f"http://127.0.0.1:{TEST_PORT}{path}", data=data, method=method)
        if cookie:
            req.add_header("Cookie", f"jb={jobboard.TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def fetch_full(self, path, cookie=True):
        """Like fetch() but also returns the response headers."""
        req = urllib.request.Request(f"http://127.0.0.1:{TEST_PORT}{path}")
        if cookie:
            req.add_header("Cookie", f"jb={jobboard.TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")

    def test_pages_need_the_cookie(self):
        for path in ("/", f"/job/{self.jid}", f"/api/status/{self.jid}", "/api/statuses"):
            code, _ = self.fetch(path, cookie=False)
            self.assertEqual(code, 403, path)

    def test_pages_ok_with_cookie(self):
        for path in ("/", f"/job/{self.jid}", f"/api/status/{self.jid}", "/api/statuses"):
            code, _ = self.fetch(path)
            self.assertEqual(code, 200, path)

    def test_job_page_has_banner_and_checklist(self):
        code, h = self.fetch(f"/job/{self.jid}")
        self.assertEqual(code, 200)
        self.assertIn('id="banner"', h)
        self.assertIn('class="btitle s-FINISHED"', h)  # the big status headline
        self.assertIn('class="bar s-FINISHED"', h)     # the thin progress bar
        self.assertIn("<h2>Job</h2>", h)               # job prompt in its own card
        self.assertIn('class="check"', h)          # the step checklist
        self.assertIn("write hello.sh", h)
        self.assertIn("Show details", h)           # the collapsed feed
        self.assertIn("/api/status/", h)           # the 3s poller

    def test_favicon_needs_no_cookie(self):
        code, _ = self.fetch("/favicon.ico", cookie=False)
        self.assertEqual(code, 204)

    SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)

    def test_page_scripts_parse_with_node(self):
        """Every rendered <script> must be valid JavaScript. An unbalanced brace
        inside a // comment once killed the whole job-page script, and no other
        test noticed."""
        if not shutil.which("node"):
            self.skipTest("node is not installed: cannot syntax-check the page scripts")
        for path in ("/", f"/job/{self.jid}", f"/job/{self.runid}"):  # home, finished job, running job
            code, html = self.fetch(path)
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

    def test_status_api_shapes(self):
        code, b = self.fetch(f"/api/status/{self.jid}")
        d = json.loads(b)
        self.assertEqual(d["state"], "FINISHED")
        self.assertEqual(d["steps_source"], "PLAN.md")
        self.assertEqual(d["steps_total"], 2)
        code, b = self.fetch("/api/statuses")
        d = json.loads(b)
        self.assertIn(self.jid, d["statuses"])
        self.assertEqual(d["statuses"][self.jid]["state"], "FINISHED")

    def test_events_api_still_works(self):
        code, b = self.fetch(f"/api/events/{self.jid}?since=0")
        self.assertEqual(code, 200)
        d = json.loads(b)
        self.assertTrue(d["items"])
        self.assertGreater(d["next"], 0)

    def test_post_refused_in_readonly(self):
        code, b = self.fetch("/new", method="POST",
                             data=b"mode=local-claude&folder=zz&job=hi")
        self.assertEqual(code, 403)
        self.assertIn("read-only", b)

    # ------------------------------------------------------------ review bundle

    def test_bundle_finished_job_is_markdown(self):
        code, hdr, body = self.fetch_full(f"/api/bundle/{self.jid}")
        self.assertEqual(code, 200)
        self.assertEqual(hdr.get_content_type(), "text/markdown")
        self.assertTrue(body.startswith("# Review bundle: 20261107-180819-71a7"), body[:120])
        self.assertIn("## Job prompt", body)
        self.assertIn("(bundle:", body)  # the token-count footer

    def test_bundle_running_job_refused(self):
        code, _, body = self.fetch_full(f"/api/bundle/{self.runid}")
        self.assertEqual(code, 409)
        self.assertIn("has not finished", body)

    def test_bundle_needs_the_cookie(self):
        code, _, _ = self.fetch_full(f"/api/bundle/{self.jid}", cookie=False)
        self.assertEqual(code, 403)

    def test_bundle_download_header(self):
        code, hdr, _ = self.fetch_full(f"/api/bundle/{self.jid}?download=1")
        self.assertEqual(code, 200)
        self.assertEqual(hdr.get("Content-Disposition"),
                         f'attachment; filename="review-{self.jid}.md"')
        # this test server is read-only (JOBBOARD_READONLY=1): it must not have
        # written review.md into the job records
        self.assertFalse(os.path.exists(os.path.join(_TMP_JOBS, self.jid, "review.md")))

    def test_bundle_card_only_on_finished_pages(self):
        code, h = self.fetch(f"/job/{self.jid}")
        self.assertIn("<h2>Review bundle</h2>", h)
        self.assertIn("Copy for Claude", h)
        self.assertIn(f"/api/bundle/{self.jid}?download=1", h)
        self.assertIn("paste it into Claude to review", h)
        # Firefox-safe copy: the ClipboardItem write starts synchronously in the
        # click handler (await would lose the user activation), writeText second
        self.assertIn("window.ClipboardItem", h)
        self.assertIn("new ClipboardItem", h)
        self.assertIn("writeText", h)
        code, h = self.fetch(f"/job/{self.runid}")
        self.assertNotIn("<h2>Review bundle</h2>", h)
        self.assertNotIn('id="bundlebox"', h)


if __name__ == "__main__":
    unittest.main()
