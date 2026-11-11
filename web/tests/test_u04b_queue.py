#!/usr/bin/env python3
r"""Upgrade 4b: an approved plan runs as a queue of tasks, one job at a time.

* Queue    - approving a plan writes QUEUES/<folder>.json in dependency order and leaves it
             "ready": nothing runs until Start the tasks. A queue under way is never replaced; a
             folder name that is not a folder name never becomes a path; the tasks come from the
             approved record, never from the (agent-writable) tasks.json in the folder.
* Prompt   - every task job says what the app is (a 10-line summary of the outline), what its
             milestone is for (taken from the project's ROADMAP.md), the task, its acceptance
             criteria and the AGENT_NOTES.md rule (plan.job_prompt), plus where the task sits.
* One at a time - start_next queues exactly one job; pressing start again queues nothing; the
             board's worker is what keeps them single. A task's own checks go in its record, and
             best-of-N only for size M or a task that already failed, and only when the project
             switched it on. check_record refuses a task record that lies about its fields.
* Failures - stop (the default): the queue stops and later tasks never start. skip: the tasks
             that depend on the failed one are skipped and the rest carry on. A task you stopped
             goes back to interrupted and the queue pauses; Cancel also stops the running job.
* Notes    - AGENT_NOTES.md that was not updated is noted in the job record and on the timeline.
* Restart  - startup() after the board marked its running jobs interrupted: the task becomes
             interrupted, starts again resuming that job's session, and says which checkpoint it
             continues from. A paused queue is not started by a restart.
* Cutoff   - no new task starts past "don't start new tasks after"; the card says so; a task that
             is already running still finishes.
* Branch   - keep_branch moves the project branch back after best-of-N without touching a file;
             a folder with anything uncommitted is left alone; base/end commits are recorded, so
             the chain from one task to the next is visible.
* Card     - milestone progress ("M1 - it runs: 1 of 2 tasks"), the current task, the time left,
             Pause / Resume / Cancel, the on-fail choice and the cutoff field; failed and skipped
             tasks shown plainly, with "Run it again".
* Pages    - the real handler in-process: approving builds the queue (and still runs nothing),
             /queue/<folder> starts/pauses/resumes/cancels/saves settings/retries a task, the job
             page says which task it is, the guard and unknown projects behave.
* Run      - fake-agent runs through the real process_job in a child process: tasks run in
             dependency order one after another, each starting from the commit the task before it
             left, a task's own checks run, a failure stops vs skips, best-of-N runs for a big
             task, a task that left no lessons is recorded as such, a past cutoff starts nothing,
             and a board that died mid-task runs that task again.

Nothing here calls the model server (127.0.0.1:8080) or starts a real agent.
"""
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

if "jobboard" not in sys.modules:      # standalone run: same isolated env as test_jobstatus
    _hb = tempfile.mkdtemp(prefix="jb04b-home-")
    os.environ.update({"HOME": _hb, "JOBBOARD_JOBS": os.path.join(_hb, "jobs"),
                       "JOBBOARD_WORK": os.path.join(_hb, "work"),
                       "JOBBOARD_PLANS": os.path.join(_hb, "plans"),
                       "JOBBOARD_QUEUES": os.path.join(_hb, "queues"), "JOBBOARD_READONLY": "1"})
import jobboard  # noqa: E402
import planning  # noqa: E402
import taskqueue  # noqa: E402
import upgrades  # noqa: E402
from agentjobs_upgrades import plan as pl  # noqa: E402


def git(repo, *a):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t",
                           "-c", "user.email=t@t", *a], cwd=str(repo), check=True,
                          capture_output=True, text=True).stdout


def T(i, deps=(), ms="M1 - it runs", size="S", acc=("it works",), checks=()):
    t = {"id": i, "milestone": ms, "title": "Task " + i, "prompt": "do " + i,
         "acceptance": list(acc), "depends_on": list(deps), "size": size}
    if checks:
        t["checks"] = list(checks)
    return t


PLAN = [T("t1"), T("t2", ["t1"]), T("t3", ["t2"], "M2 - it remembers")]
ROADMAP = ("# M1 - it runs\nYou can write a note and it stays there.\n\n"
           "# M2 - it remembers\nYou can find a note again by searching.\n")


def host_mode(case):
    """git runs straight on the temp folders, like the other suites."""
    p = mock.patch.object(upgrades, "HOST_MODE", True)
    p.start()
    case.addCleanup(p.stop)


def timeline(job_dir):
    p = Path(job_dir) / "timeline.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def rec_of(tasks=None, folder="my-app", **kw):
    rec = planning.new_record(folder, "My Notes", "web",
                              "A notes app for my phone.\nIt works without internet.\n"
                              "Notes stay on the phone.")
    rec["tasks"] = json.loads(json.dumps(tasks if tasks is not None else PLAN))
    rec["approved_at"] = 1000.0
    rec["approved_hash"] = planning.hash_of(rec["tasks"])
    rec["plan_status"] = "approved"
    rec.update(kw)
    return rec


class Fake:
    """The handful of board functions taskqueue calls, on temp folders: no HTTP, no worker
    thread, no model. `started` is what new_job was asked for; `metas` is the record store."""

    def __init__(self, with_git=False):
        self.tmp = Path(tempfile.mkdtemp(prefix="u04b-"))
        self.queues, self.plans = self.tmp / "queues", self.tmp / "plans"
        self.work, self.jobs = self.tmp / "work", self.tmp / "jobs"
        for d in (self.queues, self.plans, self.work, self.jobs):
            d.mkdir(parents=True)
        self.root = self.work / "my-app"
        self.root.mkdir(parents=True)
        (self.root / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        if with_git:
            git(self.root, "init", "-q", "-b", "agent/my-app")
            (self.root / "README.md").write_text("# notes\n")
            git(self.root, "add", "-A")
            git(self.root, "commit", "-qm", "start")     # a clean folder, like a finished task leaves it
        self.metas, self.started, self.stops, self.sessions = {}, [], [], {}
        self.cfg = {"attempts": 3, "stop_early": True}
        self.n = 0

    def ctx(self):
        return dict(queues=self.queues, plans=self.plans, work=self.work, new_job=self.new_job,
                    update=self.update, load=lambda jid: self.metas.get(jid),
                    session_of=lambda jid: self.sessions.get(jid), job_dir=lambda jid: self.jobs / str(jid),
                    busy=lambda f: False, modes={"local-qwen": "Local", "fake": "Fake"},
                    settings=lambda: self.cfg, default_mode="local-qwen",
                    stop=lambda jid: self.stops.append(jid))

    def record(self, rec=None):
        planning.save(self.plans, rec or rec_of())

    def new_job(self, mode, folder, job, offline, resume=None, parent=None, extra=None):
        self.n += 1
        jid = "20261111-12000%d-%04x" % (self.n % 100, self.n)
        m = {"id": jid, "mode": mode, "folder": folder, "job": job, "offline": offline,
             "status": "queued", "resume": resume, "parent": parent,
             "created": time.time(), "started": None, "finished": None}
        m.update(extra or {})
        (self.jobs / jid).mkdir(exist_ok=True)
        self.metas[jid] = m
        self.started.append(m)
        return m

    def update(self, jid, **kw):
        m = self.metas.get(jid)
        if m is not None:
            m.update(kw)
        return m

    def queue(self):
        return taskqueue.load(self.queues, "my-app")

    def start(self):
        """Press Start the tasks (the card's button)."""
        return taskqueue.action("my-app", {"act": ["start"]}, self.ctx())

    def finish(self, m, status="done"):
        """What the board does when a job that came from the queue has ended."""
        m = dict(m, status=status, started=m.get("started") or (time.time() - 600), finished=time.time())
        self.metas[m["id"]] = m
        return taskqueue.job_finished(m, self.ctx())


# ------------------------------------------------------------------ the queue file

class QueueFile(unittest.TestCase):
    def setUp(self):
        self.b = Fake()

    def test_approving_makes_a_queue_in_dependency_order_and_runs_nothing(self):
        q = taskqueue.create(self.b.queues, rec_of([T("t3", ["t1"]), T("t1"), T("t2", ["t1"])]))
        self.assertEqual([t["id"] for t in q.state["tasks"]], ["t1", "t3", "t2"])   # t1 first, always
        self.assertEqual(q.state["status"], "ready")
        self.assertEqual(q.state["on_fail"], "stop")
        self.assertTrue(all(t["state"] == "queued" for t in q.state["tasks"]))
        self.assertTrue((self.b.queues / "my-app.json").is_file())
        self.assertIsNone(q.next_task())                       # nothing may start yet
        self.assertIsNone(taskqueue.start_next("my-app", self.b.ctx()))
        self.assertEqual(self.b.started, [])

    def test_the_queue_is_on_disk_and_survives_a_reload(self):
        taskqueue.create(self.b.queues, rec_of())
        again = taskqueue.load(self.b.queues, "my-app")
        self.assertEqual([t["id"] for t in again.state["tasks"]], ["t1", "t2", "t3"])

    def test_a_queue_under_way_is_not_replaced_by_approving_again(self):
        taskqueue.create(self.b.queues, rec_of())
        self.b.record()
        self.b.start()
        q = taskqueue.create(self.b.queues, rec_of([T("x")]))
        self.assertEqual([t["id"] for t in q.state["tasks"]], ["t1", "t2", "t3"])

    def test_a_finished_queue_is_replaced_when_the_plan_is_approved_again(self):
        q = taskqueue.create(self.b.queues, rec_of([T("t1")]))
        q.state["status"] = "finished"
        q.save()
        q = taskqueue.create(self.b.queues, rec_of([T("a"), T("b")]))
        self.assertEqual([t["id"] for t in q.state["tasks"]], ["a", "b"])

    def test_a_folder_name_that_is_not_a_folder_name_never_becomes_a_path(self):
        for bad in ("../etc", "a/b", "", ".hidden", None):
            self.assertIsNone(taskqueue.q_path(self.b.queues, bad), bad)
            self.assertIsNone(taskqueue.load(self.b.queues, bad), bad)
        self.assertIsNone(taskqueue.create(self.b.queues, {"folder": "../etc", "tasks": PLAN}))
        self.assertFalse((self.b.tmp / "etc.json").exists())

    def test_a_file_that_is_not_a_queue_is_refused_not_used(self):
        (self.b.queues / "my-app.json").write_text("[not, a, queue]")
        self.assertIsNone(taskqueue.load(self.b.queues, "my-app"))

    def test_the_tasks_come_from_the_approved_record_not_the_folder(self):
        (self.b.root / "tasks.json").write_text(json.dumps([T("evil", ms="x")]), encoding="utf-8")
        q = taskqueue.create(self.b.queues, rec_of())
        self.assertEqual([t["id"] for t in q.state["tasks"]], ["t1", "t2", "t3"])


# ------------------------------------------------------------------ the task's prompt

class Prompt(unittest.TestCase):
    def setUp(self):
        self.b = Fake()
        self.rec = rec_of(outline="\n".join(f"line {i}" for i in range(1, 16)))
        self.b.record(self.rec)

    def prompt(self, task, pos=2, total=3):
        return taskqueue.task_prompt(self.rec, self.b.root, task, pos, total)

    def test_the_outline_summary_is_ten_lines_at_most(self):
        self.assertEqual(taskqueue.outline_summary(self.rec).splitlines(),
                         [f"line {i}" for i in range(1, 11)])
        self.assertNotIn("line 11", self.prompt(T("t1")))

    def test_the_milestone_comes_from_the_project_s_roadmap(self):
        p = self.prompt(T("t3", ms="M2 - it remembers"))
        self.assertIn("You can find a note again by searching.", p)
        self.assertNotIn("You can write a note and it stays there.", p)

    def test_the_roadmap_heading_may_be_written_differently(self):
        (self.b.root / "ROADMAP.md").write_text("## M2: it remembers\nSearching finds a note.\n")
        self.assertIn("Searching finds a note.", self.prompt(T("t3", ms="M2 - it remembers")))

    def test_without_a_roadmap_the_milestone_and_its_tasks_are_used(self):
        (self.b.root / "ROADMAP.md").unlink()
        self.assertIn("M2 - it remembers", self.prompt(T("t3", ms="M2 - it remembers")))

    def test_the_prompt_carries_the_task_its_criteria_and_the_notes_rule(self):
        p = self.prompt(T("t2", ["t1"], acc=["search finds a note"]))
        self.assertIn("Your task: Task t2", p)
        self.assertIn("do t2", p)
        self.assertIn("- search finds a note", p)
        self.assertIn("Read AGENT_NOTES.md first", p)
        self.assertIn("add 2-5 lines of lessons", p)
        self.assertIn("You are task 2 of 3 in a plan Jason already approved", p)

    def test_a_screen_task_says_what_the_screen_looks_like(self):
        t = dict(T("t1"), screen="A list of notes, a plus button bottom right")
        self.assertIn("plus button bottom right", self.prompt(t))


# ------------------------------------------------------------------ one job at a time

class Start(unittest.TestCase):
    def setUp(self):
        self.b = Fake()
        self.b.record()
        taskqueue.create(self.b.queues, rec_of())

    def test_pressing_start_queues_one_job_for_the_first_task(self):
        self.assertEqual(self.b.start(), "started")
        self.assertEqual(len(self.b.started), 1)
        m = self.b.started[0]
        self.assertEqual((m["folder"], m["mode"], m["task_id"], m["task_try"]),
                         ("my-app", "local-qwen", "t1", 1))
        self.assertEqual(self.b.queue().state["status"], "running")
        t = taskqueue.entry(self.b.queue(), "t1")
        self.assertEqual((t["state"], t["job_id"]), ("running", m["id"]))
        self.assertTrue(any("Task 1 of 3" in e["text"] for e in timeline(self.b.jobs / m["id"])))

    def test_pressing_start_again_queues_nothing_because_a_task_is_running(self):
        self.b.start()
        self.b.start()
        self.assertEqual(len(self.b.started), 1)

    def test_the_next_task_waits_for_the_one_before_it(self):
        self.b.start()
        m = self.b.started[0]
        taskqueue.start_next("my-app", self.b.ctx())           # nothing may start while t1 runs
        self.assertEqual(len(self.b.started), 1)
        self.b.finish(m)                                       # t1 done -> t2 queued
        self.assertEqual([x["task_id"] for x in self.b.started], ["t1", "t2"])
        self.assertEqual(self.b.started[1]["parent"], m["id"])  # the chain shows on the board

    def test_a_small_task_that_never_failed_is_tried_once_even_when_the_project_wants_best_of_n(self):
        self.b.record(rec_of(best_of_n=True))
        taskqueue.create(self.b.queues, rec_of(best_of_n=True))
        self.b.start()
        m = self.b.started[0]
        self.assertEqual(m["mode"], "local-qwen")              # rec["mode"] unset -> the default
        self.assertIsNone(m.get("attempts"))                   # t1 is size S and never failed

    def test_a_task_that_has_checks_gets_them_in_its_record(self):
        tasks = [T("t1", checks=["python3 -m pytest -q", "bash build.sh"])]
        self.b.record(rec_of(tasks))
        taskqueue.create(self.b.queues, rec_of(tasks))
        self.b.start()
        m = self.b.started[0]
        self.assertEqual(m["checks_source"], "the approved plan")
        self.assertEqual([c["cmd"] for c in m["checks"]], ["python3 -m pytest -q", "bash build.sh"])
        self.assertIsNone(jobboard.check_record(m["id"], m))   # the record passes the safety check

    def test_a_task_record_that_lies_about_its_task_fields_is_refused(self):
        self.b.start()
        m = self.b.started[0]
        for bad, why in (({"task_id": "../etc"}, "task_id"), ({"task_id": "x" * 50}, "task_id"),
                         ({"task_id": 7}, "task_id"), ({"task_try": 0}, "task_try"),
                         ({"task_try": "3"}, "task_try"), ({"notes_before": "z" * 200}, "notes_before")):
            self.assertEqual(jobboard.check_record(m["id"], dict(m, **bad)), why, bad)


# ------------------------------------------------------------------ best-of-N for tasks

class BestOfN(unittest.TestCase):
    def test_only_big_tasks_and_tasks_that_already_failed_are_tried_several_times(self):
        on = {"best_of_n": True}
        self.assertEqual(taskqueue.best_of_n(T("a", size="M"), on, {"attempts": 3}), 3)
        self.assertIsNone(taskqueue.best_of_n(T("a", size="S"), on, {"attempts": 3}))
        self.assertEqual(taskqueue.best_of_n(T("a", size="S"), on, {"attempts": 3}, tries=1), 3)
        self.assertIsNone(taskqueue.best_of_n(T("a", size="M"), {}, {"attempts": 3}))

    def test_the_number_of_tries_stays_inside_what_upgrade_5_allows(self):
        on = {"best_of_n": True}
        self.assertEqual(taskqueue.best_of_n(T("a", size="M"), on, {"attempts": 9}), 4)
        self.assertEqual(taskqueue.best_of_n(T("a", size="M"), on, {"attempts": 1}), 2)
        self.assertEqual(taskqueue.best_of_n(T("a", size="M"), on, {}), 3)

    def test_a_task_that_failed_before_is_tried_several_times_when_it_runs_again(self):
        self.b = Fake()
        self.b.record(rec_of(best_of_n=True))
        taskqueue.create(self.b.queues, rec_of(best_of_n=True))
        self.b.start()
        self.b.finish(self.b.started[0], status="failed")      # on_fail stop
        self.assertEqual(self.b.queue().state["status"], "stopped")
        taskqueue.action("my-app", {"act": ["retry"], "task": ["t1"]}, self.b.ctx())
        again = [m for m in self.b.started if m["task_id"] == "t1"][-1]
        self.assertEqual((again["task_try"], again["attempts"], again["hard"]), (2, 3, False))


# ------------------------------------------------------------------ AGENT_NOTES.md

class Notes(unittest.TestCase):
    def setUp(self):
        self.b = Fake()
        self.b.record()
        taskqueue.create(self.b.queues, rec_of())

    def test_a_task_that_wrote_its_lessons_gets_no_note(self):
        self.b.start()
        m = self.b.started[0]
        self.assertEqual(m["notes_before"], "")                # no file when the task started
        (self.b.root / "AGENT_NOTES.md").write_text("- the SDK needs a flag\n")
        self.b.finish(m)
        self.assertNotIn("notes_note", self.b.metas[m["id"]])

    def test_a_task_that_changed_nothing_is_recorded_and_said_so(self):
        (self.b.root / "AGENT_NOTES.md").write_text("- old lesson\n")
        self.b.start()
        m = self.b.started[0]
        self.assertEqual(len(m["notes_before"]), 64)
        self.b.finish(m)
        self.assertIn("AGENT_NOTES.md was not changed", self.b.metas[m["id"]]["notes_note"])
        self.assertTrue(any("AGENT_NOTES.md" in e["text"] for e in timeline(self.b.jobs / m["id"])))

    def test_a_project_with_no_notes_file_at_all_says_so(self):
        self.assertEqual(taskqueue.notes_note(self.b.root, ""),
                         "No AGENT_NOTES.md in the project folder - this task wrote down no lessons.")
        self.assertEqual(taskqueue.notes_hash(self.b.root), "")

    def test_a_stopped_task_is_not_blamed_for_no_lessons(self):
        self.b.start()
        m = self.b.started[0]
        self.b.finish(m, status="stopped")
        self.assertNotIn("notes_note", self.b.metas[m["id"]])


# ------------------------------------------------------------------ a task fails

class Failing(unittest.TestCase):
    def setUp(self):
        self.tasks = [T("t1"), T("t2", ["t1"]), T("t3")]      # t3 depends on nothing
        self.b = Fake()
        self.b.record(rec_of(self.tasks))

    def test_stop_is_the_default_and_freezes_the_queue(self):
        taskqueue.create(self.b.queues, rec_of(self.tasks))
        self.b.start()
        jid = self.b.started[0]["id"]
        self.b.finish(self.b.started[0], status="failed")
        q = self.b.queue()
        self.assertEqual((q.state["status"], taskqueue.entry(q, "t1")["state"]), ("stopped", "failed"))
        self.assertEqual([t["state"] for t in q.state["tasks"] if t["id"] != "t1"], ["queued", "queued"])
        self.assertEqual(len(self.b.started), 1)               # nothing else started
        self.assertTrue(any("the queue stopped" in e["text"] for e in timeline(self.b.jobs / jid)))

    def test_skip_skips_the_tasks_that_depend_on_it_and_carries_on(self):
        taskqueue.create(self.b.queues, rec_of(self.tasks), on_fail="skip")
        self.b.start()
        self.b.finish(self.b.started[0], status="failed")
        q = self.b.queue()
        self.assertEqual([t["state"] for t in q.state["tasks"]], ["failed", "skipped", "running"])
        self.assertEqual([m["task_id"] for m in self.b.started], ["t1", "t3"])

    def test_a_safety_stop_counts_as_a_failed_task(self):
        taskqueue.create(self.b.queues, rec_of(self.tasks))
        self.b.start()
        self.b.finish(self.b.started[0], status="safety_stop")
        self.assertEqual(taskqueue.entry(self.b.queue(), "t1")["state"], "failed")
        self.assertEqual(self.b.queue().state["status"], "stopped")

    def test_a_task_you_stopped_goes_back_and_the_queue_pauses(self):
        taskqueue.create(self.b.queues, rec_of(self.tasks))
        self.b.start()
        self.b.finish(self.b.started[0], status="stopped")
        q = self.b.queue()
        self.assertEqual((q.state["status"], taskqueue.entry(q, "t1")["state"]), ("paused", "interrupted"))
        self.assertEqual(len(self.b.started), 1)
        taskqueue.action("my-app", {"act": ["resume"]}, self.b.ctx())
        second = [m for m in self.b.started if m["task_id"] == "t1"][-1]
        self.assertEqual(second["task_try"], 2)

    def test_cancelling_the_queue_stops_the_job_that_is_running(self):
        taskqueue.create(self.b.queues, rec_of(self.tasks))
        self.b.start()
        jid = self.b.started[0]["id"]
        self.assertEqual(taskqueue.action("my-app", {"act": ["cancel"]}, self.b.ctx()), "cancelled")
        self.assertEqual(self.b.queue().state["status"], "cancelled")
        self.assertEqual(self.b.stops, [jid])


# ------------------------------------------------------------------ restarts and the cutoff

class RestartAndCutoff(unittest.TestCase):
    def setUp(self):
        self.b = Fake()
        self.b.record()
        taskqueue.create(self.b.queues, rec_of())

    def test_a_task_that_was_running_when_the_board_died_starts_again(self):
        self.b.start()
        m = self.b.started[0]
        self.b.sessions[m["id"]] = "sess-12345678"
        self.b.update(m["id"], status="running", started=time.time() - 300,
                      checkpoints={"checkpoints": [{"n": 1, "commit": "abc123def"}]})
        started = taskqueue.startup(self.b.queues, self.b.ctx())
        self.assertEqual(len(started), 1)
        again = self.b.started[-1]
        self.assertEqual((again["task_id"], again["task_try"], again["parent"]), ("t1", 2, m["id"]))
        self.assertEqual(again["resume"], "sess-12345678")     # it carries that job's conversation on
        self.assertEqual(taskqueue.entry(self.b.queue(), "t1")["state"], "running")

    def test_startup_logs_the_checkpoint_a_task_continues_from(self):
        self.b.start()
        m = self.b.started[0]
        self.b.update(m["id"], status="running",
                      checkpoints={"checkpoints": [{"n": 2, "commit": "ff00aa"}]})
        taskqueue.startup(self.b.queues, self.b.ctx())
        self.assertEqual(taskqueue.entry(self.b.queue(), "t1")["resume_from"], "ff00aa")
        self.assertTrue(any("starts again from checkpoint 2" in e["text"]
                            for e in timeline(self.b.jobs / m["id"])))

    def test_a_paused_queue_is_not_started_by_a_restart(self):
        self.b.start()
        taskqueue.action("my-app", {"act": ["pause"]}, self.b.ctx())
        taskqueue.startup(self.b.queues, self.b.ctx())
        self.assertEqual(self.b.queue().state["status"], "paused")
        self.assertEqual(len(self.b.started), 1)

    def test_no_new_task_starts_after_the_chosen_time(self):
        q = self.b.queue()
        q.state["stop_starting_at"] = time.time() - 1
        q.state["status"] = "running"
        q.save()
        self.assertIsNone(taskqueue.start_next("my-app", self.b.ctx()))
        self.assertEqual(self.b.started, [])
        self.assertIn("No new task starts after", taskqueue.idle_reason(q))

    def test_resume_after_that_time_starts_the_next_task_again(self):
        self.b.start()
        m = self.b.started[0]
        q = self.b.queue()
        q.state["stop_starting_at"] = time.time() - 1
        q.save()
        self.assertIsNone(self.b.finish(m))                     # nothing started at the time
        self.assertEqual(taskqueue.action("my-app", {"act": ["resume"]}, self.b.ctx()), "resumed")
        self.assertIsNone(self.b.queue().state["stop_starting_at"])   # Resume means carry on now
        self.assertEqual([x["task_id"] for x in self.b.started], ["t1", "t2"])

    def test_a_task_finishes_but_its_successor_waits_for_that_time(self):
        self.b.start()
        m = self.b.started[0]
        q = self.b.queue()
        q.state["stop_starting_at"] = time.time() - 1         # the time came round while t1 ran
        q.save()
        self.assertIsNone(self.b.finish(m))                    # t1 done, t2 not started
        self.assertEqual(taskqueue.entry(self.b.queue(), "t2")["state"], "queued")
        self.assertEqual(len(self.b.started), 1)

    def test_the_cutoff_field_saves_a_time_and_refuses_a_bad_one(self):
        ctx = self.b.ctx()
        taskqueue.action("my-app", {"act": ["setup"], "after": ["06:30"]}, ctx)
        cut = self.b.queue().state["stop_starting_at"]
        self.assertTrue(cut and cut > time.time())
        self.assertEqual(time.strftime("%H:%M", time.localtime(cut)), "06:30")
        self.assertEqual(taskqueue.action("my-app", {"act": ["setup"], "after": ["25:99"]}, ctx), "badtime")
        taskqueue.action("my-app", {"act": ["setup"], "after": [""]}, ctx)
        self.assertIsNone(self.b.queue().state["stop_starting_at"])

    def test_the_settings_button_saves_the_agent_and_the_on_fail_choice(self):
        taskqueue.action("my-app", {"act": ["setup"], "mode": ["fake"], "onfail": ["skip"],
                                    "best": ["1"]}, self.b.ctx())
        self.assertEqual(self.b.queue().state["on_fail"], "skip")
        self.assertTrue(planning.load(self.b.plans, "my-app")["best_of_n"])
        self.assertEqual(taskqueue.task_mode(planning.load(self.b.plans, "my-app"), self.b.ctx()), "fake")
        taskqueue.action("my-app", {"act": ["setup"], "onfail": ["nonsense"]}, self.b.ctx())
        self.assertEqual(self.b.queue().state["on_fail"], "skip")


# ------------------------------------------------------------------ the project branch

class Branch(unittest.TestCase):
    def setUp(self):
        self.b = Fake(with_git=True)
        host_mode(self)
        self.base = git(self.b.root, "rev-parse", "HEAD").strip()

    def test_the_project_branch_follows_a_best_of_n_winner_without_touching_files(self):
        git(self.b.root, "checkout", "-q", "-B", "agent/20261111-120000-ab12")
        self.assertEqual(taskqueue.keep_branch(self.b.root, "my-app"), "")
        self.assertEqual(git(self.b.root, "rev-parse", "--abbrev-ref", "HEAD").strip(), "agent/my-app")
        self.assertEqual(git(self.b.root, "rev-parse", "HEAD").strip(), self.base)

    def test_a_folder_that_is_already_on_its_branch_needs_nothing(self):
        self.assertEqual(taskqueue.keep_branch(self.b.root, "my-app"), "")

    def test_a_folder_with_anything_uncommitted_is_left_alone(self):
        git(self.b.root, "checkout", "-q", "-B", "agent/20261111-120000-ab12")
        (self.b.root / "half-done.py").write_text("x = 1\n")
        self.assertIn("uncommitted", taskqueue.keep_branch(self.b.root, "my-app"))
        self.assertEqual(git(self.b.root, "rev-parse", "--abbrev-ref", "HEAD").strip(),
                         "agent/20261111-120000-ab12")
        self.assertEqual((self.b.root / "half-done.py").read_text(), "x = 1\n")

    def test_the_base_and_end_commits_of_a_task_are_recorded(self):
        self.b.record()
        taskqueue.create(self.b.queues, rec_of())
        self.b.start()
        self.assertEqual(taskqueue.entry(self.b.queue(), "t1")["base"], self.base)
        (self.b.root / "note.py").write_text("notes = []\n")
        git(self.b.root, "add", "-A")
        git(self.b.root, "commit", "-qm", "t1: notes list")
        end = git(self.b.root, "rev-parse", "HEAD").strip()
        self.b.finish(self.b.started[0])
        self.assertEqual(taskqueue.entry(self.b.queue(), "t1")["end"], end)
        self.assertEqual(taskqueue.entry(self.b.queue(), "t2")["base"], end)   # chained

    def test_a_folder_that_is_not_a_git_repository_is_not_a_problem(self):
        self.assertEqual(taskqueue.keep_branch(self.b.work / "not-repo", "not-repo"), "")
        self.assertEqual(taskqueue.head_sha(self.b.work / "not-repo", "not-repo"), "")


# ------------------------------------------------------------------ the card

class Card(unittest.TestCase):
    def setUp(self):
        self.b = Fake()
        self.rec = rec_of()
        self.b.record(self.rec)
        taskqueue.create(self.b.queues, self.rec)

    def html(self):
        return taskqueue.card_html(self.rec, self.b.ctx(),
                                   "<option value='local-qwen'>Local</option>")

    def test_it_says_where_the_plan_is_milestone_by_milestone(self):
        self.b.start()
        self.b.finish(self.b.started[0])
        html = self.html()
        self.assertIn("M1 - it runs: 1 of 2 tasks", html)
        self.assertIn("M2 - it remembers: 0 of 1 task<", html)
        self.assertIn("1 of 3 tasks done", html)
        self.assertIn("Now:", html)
        self.assertIn("Task t2", html)

    def test_it_gives_a_rough_time_left_using_what_tasks_actually_took(self):
        rec = rec_of([T("s", size="S"), T("m", size="M")])
        q = taskqueue.create(self.b.queues, rec)
        self.assertEqual(taskqueue.estimate(q, rec, {"attempts": 3}), (2, 65))   # 20 + 45
        q.state["tasks"][0]["seconds"] = 600                    # an S task really took 10 min
        q.state["tasks"][0]["state"] = "done"
        self.assertEqual(taskqueue.estimate(q, rec, {"attempts": 3}), (1, 45))
        self.assertEqual(taskqueue.fmt_mins(105), "about 1h 45m")
        self.assertEqual(taskqueue.fmt_mins(0), "nothing left")

    def test_a_big_task_is_counted_once_per_try(self):
        rec = rec_of([T("s", size="S"), T("m", size="M")], best_of_n=True)
        q = taskqueue.create(self.b.queues, rec)
        self.assertEqual(taskqueue.estimate(q, rec, {"attempts": 3})[1], 20 + 45 * 3)

    def test_failed_and_skipped_tasks_are_plainly_shown(self):
        rec = rec_of([T("a"), T("b", ["a"])])
        taskqueue.create(self.b.queues, rec, on_fail="skip")
        q = self.b.queue()
        q.state["tasks"][0]["state"] = "failed"
        q.state["tasks"][1]["state"] = "skipped"
        q.save()
        html = taskqueue.card_html(rec, self.b.ctx(), "")
        self.assertIn(">failed<", html)
        self.assertIn(">skipped<", html)
        self.assertIn("Run it again", html)
        self.assertIn("2 failed or skipped", html)

    def test_the_buttons_match_the_state_of_the_queue(self):
        self.assertIn("Start the tasks", self.html())
        self.assertNotIn('value="pause"', self.html())
        self.b.start()
        html = self.html()
        self.assertIn('value="pause"', html)
        self.assertIn('value="cancel"', html)
        self.assertNotIn("Start the tasks", html)
        taskqueue.action("my-app", {"act": ["pause"]}, self.b.ctx())
        self.assertIn('value="resume"', self.html())

    def test_the_settings_offer_the_on_fail_choice_and_the_cutoff_time(self):
        html = self.html()
        for want in ("Stop the queue", "Skip the tasks that depend on it", 'name="after"',
                     "Don't start new tasks after", 'name="best"',
                     "Try big tasks (and ones that failed before) 3 times"):
            self.assertIn(want, html)

    def test_no_queue_no_card(self):
        (self.b.queues / "my-app.json").unlink()       # a project with no approved plan queue
        self.assertEqual(self.html(), "")


# ------------------------------------------------------------------ the real pages

class Pages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="jb04b-web-"))
        cls.saved = (jobboard.JOBS, jobboard.WORK, jobboard.PLANS, jobboard.QUEUES,
                     jobboard.PORT, jobboard.READONLY)
        jobboard.JOBS = cls.tmp / "jobs"
        jobboard.WORK = cls.tmp / "work"
        jobboard.PLANS = cls.tmp / "plans"
        jobboard.QUEUES = cls.tmp / "queues"
        for d in (jobboard.JOBS, jobboard.WORK, jobboard.PLANS, jobboard.QUEUES):
            d.mkdir(parents=True)
        jobboard.READONLY = False
        cls.host = mock.patch.object(upgrades, "HOST_MODE", True)
        cls.host.start()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()
        jobboard.PORT = cls.port                       # guard() reads this at request time
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), jobboard.H)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thr.join(timeout=5)
        cls.host.stop()
        (jobboard.JOBS, jobboard.WORK, jobboard.PLANS, jobboard.QUEUES,
         jobboard.PORT, jobboard.READONLY) = cls.saved

    def setUp(self):
        shutil.rmtree(jobboard.JOBS, ignore_errors=True)      # one test's jobs are not another's
        jobboard.JOBS.mkdir(parents=True)
        self.folder = "q" + re.sub(r"[^a-z0-9]", "", self.id().split(".")[-1].lower())[:40]
        self.rec = rec_of(PLAN, folder=self.folder)
        planning.save(jobboard.PLANS, self.rec)
        root = jobboard.WORK / self.folder
        root.mkdir(parents=True, exist_ok=True)
        (root / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        planning.write_tasks(root, self.rec["tasks"])          # as approving a plan leaves it

    def fetch(self, path, data=None, cookie=True):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body)
        if cookie:
            req.add_header("Cookie", f"jb={jobboard.TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.geturl()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), ""

    def queue(self):
        return taskqueue.load(jobboard.QUEUES, self.folder)

    def press(self, act, **fields):
        return self.fetch(f"/queue/{self.folder}", dict({"act": act}, **fields))

    # ---- approving makes the queue, and still runs nothing

    def test_approving_a_plan_builds_the_queue_and_still_starts_nothing(self):
        code, html, _url = self.fetch(f"/plan/edit/{self.folder}", {"tid1": "t1", "title1": "one",
                                                                  "prompt1": "p", "acc1": "ok",
                                                                  "approve": "1"})
        self.assertEqual(code, 200)
        self.assertIn("Plan approved. Nothing runs until you start the tasks.", html)
        q = self.queue()
        self.assertEqual([t["id"] for t in q.state["tasks"]], ["t1"])
        self.assertEqual(q.state["status"], "ready")
        self.assertEqual(jobboard.all_jobs(), [])
        self.assertIn("Start the tasks", html)                    # the card is right there

    def test_the_project_page_shows_the_card_with_progress_and_controls(self):
        taskqueue.create(jobboard.QUEUES, self.rec)
        code, html, _ = self.fetch(f"/project/{self.folder}")
        self.assertEqual(code, 200)
        for want in ("<h2>Tasks ", "ready to start", "0 of 3 tasks done", "M1 - it runs: 0 of 2 tasks",
                     "M2 - it remembers: 0 of 1 task<", "Start the tasks", "Don't start new tasks after",
                     "Stop the queue", "Skip the tasks that depend on it", "Task t1", ">waiting<"):
            self.assertIn(want, html)
        self.assertNotIn("Now:", html)                            # nothing is running

    # ---- the buttons

    def test_start_queues_one_job_and_the_page_says_so(self):
        taskqueue.create(jobboard.QUEUES, self.rec)
        code, html, url = self.press("start")
        self.assertEqual(code, 200)
        self.assertIn("note=started", url)
        self.assertIn("The tasks are queued - they run one at a time.", html)
        jobs = jobboard.all_job_dirs()
        self.assertEqual(len(jobs), 1)
        m = jobs[0][1]
        self.assertEqual((m["status"], m["task_id"], m["folder"]), ("queued", "t1", self.folder))
        self.assertIn("Read AGENT_NOTES.md first", m["job"])
        self.assertIn("You can write a note and it stays there.", m["job"])    # its milestone
        self.assertEqual(taskqueue.entry(self.queue(), "t1")["state"], "running")
        self.assertIn("Now:", self.fetch(f"/project/{self.folder}")[1])

    def test_a_second_start_press_queues_nothing_more(self):
        taskqueue.create(jobboard.QUEUES, self.rec)
        self.press("start")
        self.press("start")
        self.assertEqual(len(jobboard.all_jobs()), 1)

    def test_pause_resume_and_cancel(self):
        taskqueue.create(jobboard.QUEUES, self.rec)
        self.press("start")
        code, html, url = self.press("pause")
        self.assertIn("note=paused", url)
        self.assertEqual(self.queue().state["status"], "paused")
        self.assertIn("The task that is running finishes", html)
        self.assertIn("note=resumed", self.press("resume")[2])
        self.assertEqual(self.queue().state["status"], "running")
        self.assertIn("note=cancelled", self.press("cancel")[2])
        self.assertEqual(self.queue().state["status"], "cancelled")

    def test_the_settings_button_saves_the_agent_the_choice_and_the_time(self):
        taskqueue.create(jobboard.QUEUES, self.rec)
        code, html, url = self.press("setup", mode="local-qwen", onfail="skip", after="23:45", best="1")
        self.assertEqual(code, 200)
        self.assertIn("note=saved", url)
        self.assertEqual(self.queue().state["on_fail"], "skip")
        self.assertEqual(time.strftime("%H:%M", time.localtime(
            self.queue().state["stop_starting_at"])), "23:45")
        self.assertTrue(planning.load(jobboard.PLANS, self.folder)["best_of_n"])
        self.assertIn("Queue settings saved.", html)

    def test_a_time_that_is_not_a_time_is_refused(self):
        taskqueue.create(jobboard.QUEUES, self.rec)
        _code, html, url = self.press("setup", after="half past five")
        self.assertIn("note=badtime", url)
        self.assertIn("Use a time like 06:30.", html)
        self.assertIsNone(self.queue().state["stop_starting_at"])

    def test_run_it_again_puts_a_failed_task_back_in_the_queue(self):
        taskqueue.create(jobboard.QUEUES, self.rec)
        self.press("start")
        jid = jobboard.all_job_dirs()[0][0]
        jobboard.update_meta(jid, status="failed", finished=time.time(), started=time.time() - 60)
        taskqueue.job_finished(jobboard.load_meta(jid), jobboard.queue_ctx())
        self.assertEqual(self.queue().state["status"], "stopped")
        _code, _html, url = self.press("retry", task="t1")
        self.assertIn("note=retried", url)
        self.assertEqual(taskqueue.entry(self.queue(), "t1")["state"], "running")
        self.assertEqual(len([m for _, m in jobboard.all_job_dirs() if m.get("task_id") == "t1"]), 2)

    def test_the_job_page_says_which_task_it_is_and_about_missing_lessons(self):
        taskqueue.create(jobboard.QUEUES, self.rec)
        self.press("start")
        jid = jobboard.all_job_dirs()[0][0]
        jobboard.update_meta(jid, task_try=2, notes_note="AGENT_NOTES.md was not changed by this "
                                                        "task - no lessons for the tasks after it.")
        code, html, _ = self.fetch(f"/job/{jid}")
        self.assertEqual(code, 200)
        self.assertIn(f'task t1 of <a href="/project/{self.folder}">', html)
        self.assertIn("run 2 of this task", html)
        self.assertIn("AGENT_NOTES.md was not changed", html)

    def test_the_queue_of_an_unknown_project_has_no_page(self):
        self.assertEqual(self.fetch("/queue/no-such-thing", {"act": "start"})[0], 404)
        self.assertEqual(self.fetch("/project/no-such-thing")[0], 404)
        self.assertEqual(self.fetch("/queue/" + self.folder, cookie=False)[0], 403)

    def test_read_only_copies_cannot_press_the_buttons(self):
        jobboard.READONLY = True
        try:
            self.assertEqual(self.press("start")[0], 403)
        finally:
            jobboard.READONLY = False


# ------------------------------------------------------------------ a plan through the fake agent

DRIVER = textwrap.dedent("""
    import json, os, subprocess, sys, threading, time
    import jobboard, planning, taskqueue
    from pathlib import Path
    spec = json.loads(sys.argv[1])
    folder = spec["folder"]
    if not (Path(jobboard.WORK) / folder).exists():
        planning.create_folder(jobboard.WORK, folder, "My Notes", "web")
    rec = planning.new_record(folder, "My Notes", "web", spec.get("outline",
                                  "A notes app for my phone.\\nIt works without internet."))
    rec.update(tasks=spec["tasks"], approved_at=1000.0, mode="fake",
               best_of_n=bool(spec.get("best_of_n")))
    planning.save(jobboard.PLANS, rec)
    root = Path(jobboard.WORK) / folder
    (root / "ROADMAP.md").write_text(spec["roadmap"], encoding="utf-8")
    if spec.get("checks_file"):
        (root / "AGENT_CHECKS.json").write_text(json.dumps({"checks": spec["checks_file"]}),
                                                encoding="utf-8")
    g = ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=plan", "-c",
         "user.email=plan@local"]
    subprocess.run(g + ["add", "-A"], cwd=str(root), check=True)
    subprocess.run(g + ["commit", "-qm", "the approved plan"], cwd=str(root), check=False)
    q = taskqueue.create(jobboard.QUEUES, rec, on_fail=spec.get("on_fail", "stop"))
    if spec.get("cutoff_past"):
        q.state["stop_starting_at"] = time.time() - 1
        q.state["status"] = "running"
        q.save()
    if spec.get("crash_after"):
        threading.Timer(spec["crash_after"], lambda: os._exit(0)).start()
    if spec.get("startup"):
        jobboard.mark_interrupted()
        taskqueue.startup(jobboard.QUEUES, jobboard.queue_ctx())
    elif spec.get("start", True):
        q.state["status"] = "running"
        q.save()
        taskqueue.start_next(folder, jobboard.queue_ctx())
    prompts, n = {}, 0
    while True:
        qd = [x for x, mm in jobboard.all_job_dirs() if mm["status"] == "queued"]
        if not qd or n >= 12:
            break
        name, mm = qd[0], jobboard.load_meta(qd[0])
        prompts[name] = {"task": mm.get("task_id"), "job": mm["job"], "checks": mm.get("checks"),
                         "attempts": mm.get("attempts"), "resume": mm.get("resume")}
        sc = spec["scenarios"].get(mm.get("task_id") or "")
        if sc is not None:
            mm["job"] = json.dumps(sc)
            jobboard.save_meta(mm, name)
        jobboard.process_job(name)
        n += 1
    qq = taskqueue.load(jobboard.QUEUES, folder)
    (jobboard.QUEUES / (folder + ".prompts.json")).write_text(json.dumps(prompts), encoding="utf-8")
    print("RESULT=" + json.dumps({"queue": qq.state,
                                  "jobs": {name: jobboard.load_meta(name)
                                           for name, _ in jobboard.all_job_dirs()}}))
""")


def DONE(what):
    """A fake agent that does the work, writes its lessons and commits, so the next task can
    start from the commit this one left."""
    return [{"write": what + ".py", "content": f"# {what}\n"},
            {"write": "AGENT_NOTES.md", "content": f"- lesson from {what}\n"},
            {"cmd": "git add -A && git -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null "
                    "commit -qm " + what, "run": True},
            {"say": "done"}]


class Runs(unittest.TestCase):
    """Queue tasks through the real process_job in a child process (host jail + fake agent)."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="u04b-run-"))
        self.home = self.t / "home"
        (self.home / "agent-sandbox" / "state").mkdir(parents=True)
        self.work, self.jobs = self.t / "work", self.t / "jobs"
        self.plans, self.queues = self.t / "plans", self.t / "queues"
        for d in (self.work, self.jobs, self.plans, self.queues):
            d.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), JOBBOARD_JOBS=str(self.jobs),
                        JOBBOARD_WORK=str(self.work), JOBBOARD_PLANS=str(self.plans),
                        JOBBOARD_QUEUES=str(self.queues), JOBBOARD_READONLY="0",
                        JOBBOARD_SETTINGS=str(self.t / "settings.json"), JOBBOARD_JAIL="host",
                        JOBBOARD_FAKE="1", PYTHONDONTWRITEBYTECODE="1")

    def boot(self, spec, timeout=300):
        spec.setdefault("folder", "notes-app")
        spec.setdefault("roadmap", ROADMAP)
        r = subprocess.run([sys.executable, "-c", DRIVER, json.dumps(spec)], cwd=str(ROOT),
                           env=self.env, capture_output=True, text=True, timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT=")][-1]
        out = json.loads(line[7:])
        self.jobs_out = {jid: m for jid, m in out["jobs"].items()}
        self.queue = out["queue"]
        self.prompts = json.loads((self.queues / (spec["folder"] + ".prompts.json")).read_text())
        return out

    def order(self):
        """task id per job, oldest job first"""
        return [self.jobs_out[j]["task_id"]
                for _c, j in sorted((m["created"], j) for j, m in self.jobs_out.items())]

    def task_runs(self, tid):
        return [m for m in self.jobs_out.values() if m.get("task_id") == tid]

    def test_tasks_run_in_dependency_order_one_after_another(self):
        self.boot({"tasks": [T("t3", ["t1"]), T("t1"), T("t2", ["t1"], "M2 - it remembers")],
                   "scenarios": {"t1": DONE("one"), "t2": DONE("two"), "t3": DONE("three")}})
        order = self.order()
        self.assertEqual(order[0], "t1")                       # nothing else may go first
        self.assertEqual(sorted(order), ["t1", "t2", "t3"])
        self.assertEqual([t["state"] for t in self.queue["tasks"]], ["done"] * 3)
        self.assertEqual(self.queue["status"], "finished")

    def test_each_task_starts_from_the_commit_the_task_before_it_left(self):
        self.boot({"tasks": [T("t1"), T("t2", ["t1"])], "scenarios": {"t1": DONE("one"),
                                                                     "t2": DONE("two")}})
        by = {t["id"]: t for t in self.queue["tasks"]}
        self.assertNotEqual(by["t1"]["base"], by["t1"]["end"])     # t1 really committed
        self.assertEqual(by["t2"]["base"], by["t1"]["end"])        # t2 starts there
        self.assertEqual(self.task_runs("t2")[0]["git_start"], by["t1"]["end"])

    def test_the_prompt_a_task_gets_is_the_plan_s_own_words(self):
        self.boot({"tasks": [T("t1"), T("t2", ["t1"], "M2 - it remembers")],
                   "scenarios": {"t1": DONE("one"), "t2": DONE("two")}})
        first = [p for p in self.prompts.values() if p["task"] == "t1"][0]["job"]
        self.assertIn("A notes app for my phone.", first)
        self.assertIn("Your task: Task t1", first)
        self.assertIn("- it works", first)
        self.assertIn("Read AGENT_NOTES.md first", first)
        second = [p for p in self.prompts.values() if p["task"] == "t2"][0]["job"]
        self.assertIn("You can find a note again by searching.", second)   # from ROADMAP.md

    def test_a_task_s_checks_run_and_a_failing_check_fails_the_task(self):
        self.boot({"tasks": [T("t1", checks=["test -f never-written.txt"]), T("t2", ["t1"])],
                   "scenarios": {"t1": DONE("one")}})
        m = self.task_runs("t1")[0]
        self.assertEqual([c["cmd"] for c in m["checks"]], ["test -f never-written.txt"])
        self.assertEqual(m["status"], "failed")
        self.assertEqual([t["state"] for t in self.queue["tasks"]], ["failed", "queued"])
        self.assertEqual(self.queue["status"], "stopped")          # and nothing else started
        self.assertEqual(self.order(), ["t1"])

    def test_a_failure_stops_the_queue_and_later_tasks_never_start(self):
        self.boot({"tasks": [T("t1"), T("t2", ["t1"]), T("t3")],
                   "scenarios": {"t1": DONE("one"), "t2": [{"exit": 1}]}})
        self.assertEqual(self.order(), ["t1", "t2"])
        self.assertEqual([t["state"] for t in self.queue["tasks"]], ["done", "failed", "queued"])
        self.assertEqual(self.queue["status"], "stopped")

    def test_with_skip_the_tasks_that_depend_on_a_failure_are_skipped_and_the_rest_carry_on(self):
        self.boot({"tasks": [T("t1"), T("t2", ["t1"]), T("t3")], "on_fail": "skip",
                   "scenarios": {"t1": DONE("one"), "t2": [{"exit": 1}], "t3": DONE("three")}})
        self.assertEqual(self.order(), ["t1", "t2", "t3"])
        self.assertEqual([t["state"] for t in self.queue["tasks"]], ["done", "failed", "done"])
        self.assertEqual(self.queue["status"], "finished")

    def test_a_task_that_left_no_lessons_is_recorded_as_such(self):
        self.boot({"tasks": [T("t1"), T("t2", ["t1"])],
                   "scenarios": {"t1": [{"write": "a.py", "content": "x = 1\n"}, {"say": "ok"}],
                                 "t2": DONE("two")}})
        self.assertIn("AGENT_NOTES.md", self.task_runs("t1")[0].get("notes_note", ""))
        self.assertNotIn("notes_note", self.task_runs("t2")[0])

    def test_a_big_task_of_a_project_that_wants_it_is_tried_several_times(self):
        self.boot({"tasks": [T("t1", size="M")], "best_of_n": True,
                   "checks_file": [{"name": "the note is there", "cmd": "test -f two.py",
                                    "timeout_s": 60}],
                   "scenarios": {"t1": {"attempts": [[{"say": "I could not work it out"}],
                                                      DONE("two"), DONE("two")]}}})
        m = self.task_runs("t1")[0]
        self.assertEqual(m["attempts"], 3)
        log = m["attempt_log"]
        self.assertEqual([a["k"] for a in log], [1, 2, 3])
        self.assertEqual([a["status"] for a in log], ["failed", "passed", "passed"])
        self.assertEqual(m["attempts_final"]["winner"], 2)              # the first clean one won
        self.assertEqual(m["status"], "done")
        self.assertEqual(self.queue["tasks"][0]["state"], "done")
        app = self.work / "notes-app"
        self.assertTrue((app / "two.py").is_file())                     # the winner's work is there
        self.assertEqual(git(app, "rev-parse", "--abbrev-ref", "HEAD").strip(), "agent/" + m["id"])
        self.assertEqual(self.queue["tasks"][0]["end"], log[1]["commit"])   # and that is recorded

    def test_no_new_task_starts_after_the_time_even_though_the_queue_is_running(self):
        self.boot({"tasks": [T("t1"), T("t2", ["t1"])], "cutoff_past": True,
                   "scenarios": {"t1": DONE("one"), "t2": DONE("two")}})
        self.assertEqual(self.jobs_out, {})
        self.assertEqual([t["state"] for t in self.queue["tasks"]], ["queued", "queued"])
        self.assertEqual(self.queue["status"], "running")          # waiting, not stopped


class Restart(unittest.TestCase):
    """A board that died mid-task: the task is interrupted and run again."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="u04b-re-"))
        self.home = self.t / "home"
        (self.home / "agent-sandbox" / "state").mkdir(parents=True)
        self.work, self.jobs = self.t / "work", self.t / "jobs"
        self.plans, self.queues = self.t / "plans", self.t / "queues"
        for d in (self.work, self.jobs, self.plans, self.queues):
            d.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), JOBBOARD_JOBS=str(self.jobs),
                        JOBBOARD_WORK=str(self.work), JOBBOARD_PLANS=str(self.plans),
                        JOBBOARD_QUEUES=str(self.queues), JOBBOARD_READONLY="0",
                        JOBBOARD_SETTINGS=str(self.t / "settings.json"), JOBBOARD_JAIL="host",
                        JOBBOARD_FAKE="1", PYTHONDONTWRITEBYTECODE="1")

    def boot(self, spec, timeout=300, want_result=True):
        spec = dict(spec, folder="notes-app", roadmap=spec.get("roadmap", ROADMAP))
        r = subprocess.run([sys.executable, "-c", DRIVER, json.dumps(spec)], cwd=str(ROOT),
                           env=self.env, capture_output=True, text=True, timeout=timeout)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT=")]
        if not want_result:                       # this board was killed mid-task: no report
            return {}
        self.assertTrue(line, r.stdout + r.stderr)
        return json.loads(line[-1][7:])

    def test_a_task_that_was_running_is_interrupted_and_run_again(self):
        slow = [{"sleep": 4}] + DONE("one")[:2] + [{"exit": 0}]
        spec = {"tasks": [T("t1"), T("t2", ["t1"])],
                "scenarios": {"t1": slow, "t2": DONE("two")}, "crash_after": 1.2}
        self.boot(spec, want_result=False)                   # this board dies while task t1 runs
        time.sleep(4.5)                                      # let its orphaned agent finish writing
        on_disk = json.loads((self.queues / "notes-app.json").read_text())
        self.assertEqual(on_disk["tasks"][0]["state"], "running")   # exactly as a crash leaves it
        out = self.boot(dict(spec, startup=True, crash_after=0))    # the board comes back up
        self.assertEqual([t["state"] for t in out["queue"]["tasks"]], ["done", "done"])
        runs = [m for m in out["jobs"].values() if m.get("task_id") == "t1"]
        self.assertEqual(len(runs), 2)                     # the interrupted one and its re-run
        first, again = sorted(runs, key=lambda m: m["created"])
        self.assertEqual(first["status"], "interrupted")   # the record says what happened to it
        self.assertEqual(again["task_try"], 2)
        self.assertTrue(again.get("resume"))               # it carries on that job's conversation
        tl = self.jobs / first["id"] / "timeline.jsonl"
        self.assertTrue(any("Interrupted by a restart" in ln for ln in tl.read_text().splitlines()))


if __name__ == "__main__":
    unittest.main()
