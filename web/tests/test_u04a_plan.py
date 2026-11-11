#!/usr/bin/env python3
r"""Upgrade 4a: an outline becomes a plan Jason can check before anything runs.

* Schema   - docs/tasks-schema.json stays exactly plan.SCHEMA; read_tasks on a real project
             folder reports a good plan, bad ones (fields, ids, dependencies, cycles), a file
             that is too big, and no file at all.
* Prompt   - prompts/planner.md is the planner prompt: no app code, ROADMAP.md + tasks.json,
             ~45-minute tasks, task 1 = skeleton + tests + AGENT_CHECKS.json, testable
             acceptance; the outline and the errors land in it, no placeholder is left behind.
* Records  - state/plans/<folder>.json (save/load/list, a folder name that is not a folder
             name, a record that lies about its folder) and the new-project form.
* Folder   - create_folder against a real temp WORK: branch agent/<name>, one commit, and the
             git commands go through the project's own jail with safegit's flags; a jail that
             is not there is reported, not raised.
* Form     - the plan form's index-named fields (parse_qs drops empty values, so same-named
             fields could never be lined up) and Up / Down / Delete / Add.
* State    - none | running | draft | invalid | approved; approval stored outside the folder;
             a plan edited after approval is a draft again.
* Finished - plan_finished: a good plan becomes a draft and nothing runs; a refused plan goes
             back to the planner once, with the errors in its prompt, and not a second time; a
             plan job the worker refused (requeue=False) leaves the project waiting for nothing.
* Run      - a canned planner output through the fake agent in a child process
             (JOBBOARD_JAIL=host + JOBBOARD_FAKE=1): the plan lands in the folder and on the
             board; a refused plan gets exactly one re-plan.
* Pages    - the real handler served in-process on its own port: the new-project page and its
             POSTs, the project page ("Make a plan" queues a job, runs nothing; a job the worker
             refused says so instead of "the planner is working"), the plan review page and its
             one POST (save / approve / move / drop / add), the guard and unknown projects.

Nothing here calls the model server (127.0.0.1:8080) or starts a real agent.
"""
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
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
    _hb = tempfile.mkdtemp(prefix="jb04a-home-")
    os.environ.update({"HOME": _hb, "JOBBOARD_JOBS": os.path.join(_hb, "jobs"),
                       "JOBBOARD_WORK": os.path.join(_hb, "work"),
                       "JOBBOARD_PLANS": os.path.join(_hb, "plans"), "JOBBOARD_READONLY": "1"})
import jobboard  # noqa: E402
import planning  # noqa: E402
import upgrades  # noqa: E402
from agentjobs_upgrades import plan as pl  # noqa: E402

REPO = ROOT.parent


def git(repo, *a):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t",
                           "-c", "user.email=t@t", *a], cwd=str(repo), check=True,
                          capture_output=True, text=True).stdout


def T(i, deps=(), ms="M1 - it runs", acc=("it works",)):
    return {"id": i, "milestone": ms, "title": "Task " + i, "prompt": "do " + i,
            "acceptance": list(acc), "depends_on": list(deps), "size": "S"}


GOOD_PLAN = [T("t1"), T("t2", ["t1"]), T("t3", ["t2"], "M2 - it remembers")]
PLAN_TEXT = json.dumps(GOOD_PLAN, indent=1)


def write_plan(root, tasks):
    (Path(root) / "tasks.json").write_text(json.dumps(tasks, indent=1), encoding="utf-8")


def host_mode(case):
    """git / checks / the fake agent run straight on the temp folders, like the other suites."""
    p = mock.patch.object(upgrades, "HOST_MODE", True)
    p.start()
    case.addCleanup(p.stop)


def timeline(job_dir):
    p = Path(job_dir) / "timeline.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ------------------------------------------------------------------ the schema and tasks.json

class Schema(unittest.TestCase):
    def test_the_docs_schema_is_the_one_the_code_checks(self):
        doc = json.loads((REPO / "docs" / "tasks-schema.json").read_text(encoding="utf-8"))
        self.assertEqual(doc, pl.SCHEMA)

    def test_a_good_plan_reads_clean(self):
        t = Path(tempfile.mkdtemp())
        write_plan(t, GOOD_PLAN)
        tasks, errs = planning.read_tasks(t)
        self.assertEqual(errs, [])
        self.assertEqual([x["id"] for x in tasks], ["t1", "t2", "t3"])

    def test_bad_plans_are_named_not_guessed(self):
        cases = [("not a list", {"tasks": []}, "non-empty list"),
                 ("not json", None, "not valid JSON"),
                 ("empty acceptance", [dict(T("t1"), acceptance=[])], "acceptance"),
                 ("unknown field", [dict(T("t1"), estimate="2h")], "unknown field"),
                 ("duplicate id", [T("t1"), T("t1")], "duplicate id"),
                 ("unknown dependency", [T("t1", ["nope"])], "unknown task"),
                 ("circular", [T("a", ["b"]), T("b", ["a"])], "circular"),
                 ("depends on itself", [T("a", ["a"])], "itself")]
        for name, data, want in cases:
            t = Path(tempfile.mkdtemp())
            if data is None:
                (t / "tasks.json").write_text("[{oops")
            else:
                (t / "tasks.json").write_text(json.dumps(data), encoding="utf-8")
            _tasks, errs = planning.read_tasks(t)
            self.assertTrue(any(want in e for e in errs), (name, errs))

    def test_a_missing_file_is_said_plainly(self):
        self.assertEqual(planning.read_tasks(Path(tempfile.mkdtemp())), ([], [planning.MISSING]))

    def test_a_huge_tasks_file_is_refused_rather_than_parsed(self):
        t = Path(tempfile.mkdtemp())
        (t / "tasks.json").write_text(json.dumps([T(f"t{i}") for i in range(4000)]))
        tasks, errs = planning.read_tasks(t)
        self.assertEqual(tasks, [])
        self.assertTrue(any("too big" in e for e in errs), errs)

    def test_the_roadmap_is_read_when_the_planner_wrote_one(self):
        t = Path(tempfile.mkdtemp())
        self.assertEqual(planning.read_roadmap(t), "")
        (t / "ROADMAP.md").write_text("# M1 - it runs\nYou can write a note and it stays.\n")
        self.assertIn("You can write a note", planning.read_roadmap(t))


# ------------------------------------------------------------------ the planner prompt

class Prompt(unittest.TestCase):
    REC = {"folder": "notes-app", "name": "My Notes", "kind": "android",
           "outline": "A notes app for my phone that works without internet."}

    def test_the_prompt_comes_from_the_file_and_carries_the_outline(self):
        p = planning.planner_prompt(self.REC)
        self.assertIn("A notes app for my phone that works without internet.", p)
        self.assertIn("My Notes", p)
        self.assertIn("Android app", p)
        self.assertIn("notes-app", p)
        self.assertNotIn("{{", p)

    def test_the_planner_rules_are_in_there(self):
        p = planning.planner_prompt(self.REC)
        for must in ("Do not write any app code", "ROADMAP.md", "tasks.json", "45 minutes",
                     "AGENT_CHECKS.json", "acceptance", "depends_on", "milestone", "screen",
                     "skeleton"):
            self.assertIn(must, p)

    def test_a_refused_plan_sends_the_errors_back_in_the_prompt(self):
        errs = ["task t3: missing 'acceptance'", "duplicate id 't2'", "circular dependency between: t1, t2"]
        p = planning.planner_prompt(self.REC, errs)
        for e in errs:
            self.assertIn(e, p)
        self.assertIn("was refused", p)
        self.assertIn("Write both files and stop", p)          # the rest of the prompt is intact

    def test_only_a_little_of_a_huge_plan_is_sent(self):
        p = planning.planner_prompt(self.REC, [f"problem {i}" for i in range(100)])
        self.assertNotIn("problem 99", p)
        self.assertIn("more", p)
        long = planning.planner_prompt(dict(self.REC, outline="y" * 90000))
        self.assertNotIn("y" * 90000, long)


# ------------------------------------------------------------------ project records

class Records(unittest.TestCase):
    def test_records_round_trip_newest_first(self):
        d = Path(tempfile.mkdtemp()) / "plans"
        for folder, at in (("alpha", 100.0), ("beta", 200.0)):
            rec = planning.new_record(folder, folder.title(), "web", "outline " + folder)
            rec["created"] = at
            planning.save(d, rec)
        self.assertEqual([r["folder"] for r in planning.projects(d)], ["beta", "alpha"])
        self.assertEqual(planning.load(d, "alpha")["outline"], "outline alpha")
        self.assertEqual(planning.projects(Path(tempfile.mkdtemp()) / "gone"), [])

    def test_a_folder_name_that_is_not_a_folder_name_never_becomes_a_path(self):
        d = Path(tempfile.mkdtemp())
        for bad in ("../etc", "a/b", "", ".hidden", "x" * 65, None, "notes/../../etc"):
            self.assertIsNone(planning.rec_path(d, bad), bad)
            self.assertIsNone(planning.load(d, bad), bad)
        self.assertFalse(planning.save(d, {"folder": "../etc"}))
        self.assertFalse((d.parent / "etc.json").exists())

    def test_a_record_that_lies_about_its_folder_is_not_used(self):
        d = Path(tempfile.mkdtemp())
        planning.save(d, planning.new_record("one", "One", "web", "x"))
        (d / "one.json").write_text(json.dumps({"folder": "two", "name": "evil"}))
        self.assertIsNone(planning.load(d, "one"))

    def test_the_new_project_form_needs_a_name_a_kind_and_an_outline(self):
        _f, errs = planning.project_form({})
        for want in ("Give the project a name", "what kind of project", "Paste the outline"):
            self.assertTrue(any(want in e for e in errs), (want, errs))
        f, errs = planning.project_form({"name": ["my-app"], "kind": ["web"], "outline": ["does things"]})
        self.assertEqual(errs, [])
        self.assertEqual((f["folder"], f["new"]), ("my-app", True))     # the name is the folder
        f, errs = planning.project_form({"name": ["my app"], "kind": ["web"], "outline": ["x"]})
        self.assertTrue(any("simple name" in e for e in errs), errs)
        f, errs = planning.project_form({"name": ["a"], "kind": ["nope"], "outline": [""]})
        self.assertEqual(len(errs), 2, errs)
        f, errs = planning.project_form({"name": ["New"], "kind": ["web"], "outline": ["x"],
                                         "folder": ["old-app"]})
        self.assertEqual((f["folder"], f["new"]), ("old-app", False))   # plan inside an old folder


# ------------------------------------------------------------------ the new folder

class Folder(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="u04a-fold-"))
        self.work = self.t / "work"
        self.real_wrap = upgrades.make_wrap
        self.cmds = []
        self.wrap = None

        def spy(root, folder, prefix):
            self.wrap = self.real_wrap(root, folder, prefix)
            return lambda path, cmd: (self.cmds.append(cmd), self.wrap(path, cmd))[1]

        p = mock.patch.object(upgrades, "make_wrap", spy)
        p.start()
        self.addCleanup(p.stop)
        host_mode(self)

    def test_a_new_project_starts_on_its_own_branch_with_one_commit(self):
        self.assertEqual(planning.create_folder(self.work, "my-app", "My Notes", "android"), "")
        root = self.work / "my-app"
        self.assertIn("My Notes", (root / "README.md").read_text())
        self.assertEqual(git(root, "rev-parse", "--abbrev-ref", "HEAD").strip(), "agent/my-app")
        self.assertEqual(git(root, "log", "--pretty=%s").strip(), "start My Notes")

    def test_the_board_reads_and_writes_git_only_through_the_folder_s_own_jail(self):
        planning.create_folder(self.work, "my-app", "My Notes", "web")
        joined = " ".join(self.cmds)
        self.assertIn("git", joined)
        self.assertIn("core.hooksPath=/dev/null", joined)   # the folder's own config is never trusted
        self.assertIn("protocol.allow=never", joined)
        with mock.patch.object(upgrades, "HOST_MODE", False):
            argv = self.real_wrap(self.work / "my-app", "my-app", "agentgit-plan")(
                self.work / "my-app", "git status")
        self.assertTrue(str(argv[0]).endswith("run-local.sh"), argv)
        self.assertIn("sandbox-run", argv)

    def test_a_folder_whose_git_did_not_start_is_reported_not_raised(self):
        def broken(*a, **k):
            raise RuntimeError("no jail here")
        with mock.patch.object(planning, "SafeGit", broken):
            err = planning.create_folder(self.work, "broken-app", "Broken", "other")
        self.assertIn("no jail here", err)
        self.assertTrue((self.work / "broken-app" / "README.md").is_file())   # the folder stands


# ------------------------------------------------------------------ the plan form

class Form(unittest.TestCase):
    def test_cards_are_read_in_order_even_when_boxes_are_empty(self):
        # the board parses POST bodies with parse_qs, which drops empty values completely
        body = urllib.parse.urlencode({
            "tid1": "t1", "milestone1": "M1", "title1": "First", "prompt1": "do it",
            "acc1": "works\r\nalso works", "checks1": "pytest -q", "after1": "t0, t9", "size1": "M",
            "tid2": "t2", "milestone2": "M1", "title2": "Second", "prompt2": "other", "acc2": "",
            "save": "1"})
        tasks = planning.form_tasks(urllib.parse.parse_qs(body))
        self.assertEqual([t["id"] for t in tasks], ["t1", "t2"])
        self.assertEqual(tasks[0]["acceptance"], ["works", "also works"])
        self.assertEqual(tasks[0]["checks"], ["pytest -q"])
        self.assertEqual(tasks[0]["depends_on"], ["t0", "t9"])
        self.assertEqual(tasks[0]["size"], "M")
        self.assertEqual(tasks[1]["acceptance"], [])           # the planner left it blank
        self.assertTrue(any("acceptance" in e for e in planning.validate(tasks)), tasks)

    def test_fields_left_out_never_shift_the_cards(self):
        tasks = planning.form_tasks(urllib.parse.parse_qs(urllib.parse.urlencode(
            {"tid1": "t1", "title1": "one", "tid2": "t2", "title2": "two", "prompt2": "p",
             "acc2": "ok", "milestone2": "M"})))
        self.assertEqual([(t["id"], t["title"]) for t in tasks], [("t1", "one"), ("t2", "two")])
        self.assertEqual(tasks[0]["milestone"], "Tasks")       # a plain list when none was given

    def test_up_down_delete_and_add_change_the_list_only(self):
        ids = lambda ts: [t["id"] for t in ts]
        self.assertEqual(ids(planning.apply_actions(list(GOOD_PLAN), {"move": ["t3:up"]})[0]),
                         ["t1", "t3", "t2"])
        self.assertEqual(ids(planning.apply_actions(list(GOOD_PLAN), {"drop": ["t2"]})[0]),
                         ["t1", "t3"])
        out, note = planning.apply_actions(list(GOOD_PLAN), {"add": ["1"]})
        self.assertEqual(ids(out), ["t1", "t2", "t3", "t4"])
        self.assertEqual(out[-1]["milestone"], "M2 - it remembers")   # joins the last milestone
        self.assertIn("Added t4", note)

    def test_buttons_about_a_task_that_is_not_there_say_so(self):
        out, note = planning.apply_actions(list(GOOD_PLAN), {"drop": ["zz"]})
        self.assertEqual([t["id"] for t in out], ["t1", "t2", "t3"])
        self.assertIn("nothing was deleted", note)
        _out, note = planning.apply_actions(list(GOOD_PLAN), {"move": ["t1:up"]})
        self.assertIn("already up", note)
        _out, note = planning.apply_actions(list(GOOD_PLAN), {"move": ["zz:down"]})
        self.assertIn("not in the plan", note)

    def test_a_new_task_gets_an_id_nobody_uses(self):
        tasks = [T("t1"), T("t4"), T("t2")]
        self.assertNotIn(planning.next_id(tasks), {t["id"] for t in tasks})


# ------------------------------------------------------------------ the state of a plan

class State(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="u04a-state-"))
        self.plans = self.t / "plans"
        self.root = self.t / "work" / "my-app"
        self.root.mkdir(parents=True)
        self.rec = planning.new_record("my-app", "My Notes", "android", "an outline")

    def st(self):
        return planning.plan_state(self.plans, self.rec, self.root)

    def test_no_plan_yet_and_a_planner_still_working(self):
        self.assertEqual(self.st()["state"], "none")
        self.rec["plan_status"] = "running"
        self.assertEqual(self.st()["state"], "running")

    def test_a_planner_that_wrote_no_file_is_said_plainly(self):
        self.rec["plan_status"] = "invalid"        # plan_finished says this when tasks.json is absent
        line, cls = planning.state_line(self.st())
        self.assertEqual(cls, "bad")
        self.assertIn("no tasks.json", line)
        self.assertNotIn("0 problem", line)        # never "the plan has 0 problems"

    def test_a_valid_plan_is_a_draft_that_nothing_runs(self):
        write_plan(self.root, GOOD_PLAN)
        st = self.st()
        self.assertEqual((st["state"], st["count"]), ("draft", 3))
        self.assertEqual(st["milestones"], ["M1 - it runs", "M2 - it remembers"])
        line, _cls = planning.state_line(st)
        self.assertIn("Nothing runs until you approve it", line)

    def test_an_invalid_plan_cannot_be_approved(self):
        write_plan(self.root, [T("t1"), T("t1")])
        st = self.st()
        self.assertEqual(st["state"], "invalid")
        errs = planning.approve(self.plans, self.rec, self.root, st["tasks"])
        self.assertTrue(any("duplicate id" in e for e in errs), errs)
        self.assertIsNone(self.rec.get("approved_at"))
        self.assertFalse((self.plans / "my-app.json").exists())

    def test_approving_keeps_a_copy_outside_the_project_folder(self):
        write_plan(self.root, GOOD_PLAN)
        self.assertEqual(planning.approve(self.plans, self.rec, self.root, GOOD_PLAN), [])
        self.assertEqual(self.st()["state"], "approved")
        saved = planning.load(self.plans, "my-app")
        self.assertTrue(saved["approved_at"])
        self.assertEqual([t["id"] for t in saved["tasks"]], ["t1", "t2", "t3"])
        self.assertEqual(saved["plan_status"], "approved")

    def test_a_plan_edited_after_approval_is_a_draft_again(self):
        write_plan(self.root, GOOD_PLAN)
        planning.approve(self.plans, self.rec, self.root, GOOD_PLAN)
        write_plan(self.root, GOOD_PLAN[:2])
        st = self.st()
        self.assertEqual(st["state"], "draft")
        self.assertTrue(st["changed"])
        self.assertIn("approved an earlier version", planning.project_body(self.rec, st, "", ""))

    def test_the_page_shows_the_milestones_as_sections(self):
        write_plan(self.root, GOOD_PLAN)
        html = planning.plan_body(self.rec, self.st(), GOOD_PLAN)
        self.assertIn("<details class=\"ms\"", html)
        self.assertIn("M2 - it remembers", html)
        self.assertIn('name="tid1"', html)
        self.assertIn("after: t2", html)                # the "after:" line the spec asks for
        self.assertIn('name="approve"', html)
        self.assertEqual(html.count('name="move"'), 6)  # up and down on every card


# ------------------------------------------------------------------ a planning job finished

class Finished(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="u04a-fin-"))
        self.plans, self.work = self.t / "plans", self.t / "work"
        (self.work / "my-app").mkdir(parents=True)
        self.jdir = self.t / "job"
        self.jdir.mkdir()
        planning.save(self.plans, planning.new_record("my-app", "My Notes", "web", "an outline"))
        self.started = []

    def rec(self, **kw):
        m = {"id": "20261111-120000-ab12", "mode": "local-qwen", "folder": "my-app",
             "status": "done", "plan_job": 1}
        m.update(kw)
        return m

    def finish(self, m, **kw):
        def new_job(mode, folder, job, offline, resume=None, parent=None, extra=None):
            self.started.append({"mode": mode, "folder": folder, "job": job, "resume": resume,
                                 "parent": parent, "extra": extra})
            return {"id": "20261111-120001-beef", "status": "queued"}
        return planning.plan_finished(m, plans=self.plans, work=self.work, new_job=new_job,
                                      session_of=lambda jid: "fake-12345678",
                                      job_dir=lambda jid: self.jdir, **kw)

    def test_any_other_job_is_left_alone(self):
        self.assertIsNone(planning.plan_finished({"id": "x", "folder": "my-app"}, plans=self.plans,
                                                 work=self.work, new_job=lambda *a, **k: None))
        self.assertIsNone(self.finish({"id": "x", "folder": "my-app", "kind": "bench"}))
        self.assertEqual(self.started, [])

    def test_a_good_plan_becomes_a_draft_and_nothing_else_runs(self):
        write_plan(self.work / "my-app", GOOD_PLAN)
        self.assertIsNone(self.finish(self.rec()))
        self.assertEqual(self.started, [])
        self.assertEqual(planning.load(self.plans, "my-app")["plan_status"], "draft")
        self.assertTrue(any("Plan ready: 3 tasks in 2 milestones" in e["text"]
                            for e in timeline(self.jdir)))

    def test_a_refused_plan_goes_back_to_the_planner_once_with_its_errors(self):
        write_plan(self.work / "my-app", [dict(T("t1", acc=())), T("t1")])
        self.assertEqual(self.finish(self.rec()), "20261111-120001-beef")
        job = self.started[0]
        self.assertEqual((job["folder"], job["parent"], job["resume"]),
                         ("my-app", "20261111-120000-ab12", "fake-12345678"))
        self.assertEqual(job["extra"]["plan_retry"], 1)
        self.assertIn("duplicate id 't1'", job["job"])
        self.assertIn("Do not write any app code", job["job"])
        self.assertEqual(self.finish(self.rec(plan_retry=1)), None)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(planning.load(self.plans, "my-app")["plan_status"], "invalid")
        self.assertTrue(any("still not valid" in e["text"] for e in timeline(self.jdir)))

    def test_a_planner_that_wrote_nothing_says_so_and_does_not_loop(self):
        self.assertIsNone(self.finish(self.rec()))
        self.assertEqual(self.started, [])
        self.assertEqual(planning.load(self.plans, "my-app")["plan_status"], "invalid")
        self.assertTrue(any("no tasks.json" in e["text"] for e in timeline(self.jdir)))

    def test_a_plan_job_that_never_ran_queues_nothing_behind_itself(self):
        """jobboard calls this with requeue=False when the worker refused the record: the project
        must stop waiting for a job that never started, and nothing may be queued for it."""
        write_plan(self.work / "my-app", [T("t1", acc=())])   # invalid: no acceptance criteria
        self.assertIsNone(self.finish(self.rec(), requeue=False))
        self.assertEqual(self.started, [])
        self.assertEqual(planning.load(self.plans, "my-app")["plan_status"], "invalid")
        self.assertTrue(any("never started" in e["text"] for e in timeline(self.jdir)))

    def test_a_record_for_a_project_that_was_never_registered_is_still_kept(self):
        write_plan(self.work / "my-app", GOOD_PLAN)
        self.plans.joinpath("my-app.json").unlink()
        self.assertIsNone(self.finish(self.rec()))
        self.assertEqual(planning.load(self.plans, "my-app")["plan_status"], "draft")


# ------------------------------------------------------------------ the fake agent writes a plan

DRIVER = textwrap.dedent("""
    import json, sys
    import jobboard, planning
    spec = json.loads(sys.argv[1])
    folder = spec["folder"]
    planning.save(jobboard.PLANS, planning.new_record(folder, "My Notes", "web",
                                                      "A notes app that works offline."))
    m = jobboard.new_job("fake", folder, json.dumps(spec["scenario"]), False,
                         extra=planning.plan_extra())
    ids = [m["id"]]
    jobboard.process_job(m["id"])
    while True:
        q = [n for n, mm in jobboard.all_job_dirs() if mm["status"] == "queued"]
        if not q:
            break
        n, mm = q[0], jobboard.load_meta(q[0])
        (jobboard.PLANS / "retry-prompt.txt").write_text(mm["job"], encoding="utf-8")
        if spec.get("retry_scenario") and mm.get("plan_retry"):
            mm["job"] = json.dumps(spec["retry_scenario"])   # fake mode needs a scenario, not a prompt
            jobboard.save_meta(mm, n)
        ids.append(n)
        jobboard.process_job(n)
    left = [n for n, mm in jobboard.all_job_dirs() if mm["status"] == "queued"]
    print("RESULT=" + json.dumps({"ids": ids, "queued": left}))
""")


class Run(unittest.TestCase):
    """One canned planner output per test, through the fake agent in a child process."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="u04a-run-"))
        self.home = self.t / "home"
        (self.home / "agent-sandbox" / "state").mkdir(parents=True)
        self.work, self.jobs, self.plans = self.t / "work", self.t / "jobs", self.t / "plans"
        for d in (self.work, self.jobs, self.plans):
            d.mkdir()

    def run_plan(self, scenario, retry=None, folder="my-app", timeout=300):
        env = dict(os.environ, HOME=str(self.home), JOBBOARD_JOBS=str(self.jobs),
                   JOBBOARD_WORK=str(self.work), JOBBOARD_PLANS=str(self.plans),
                   JOBBOARD_SETTINGS=str(self.t / "settings.json"), JOBBOARD_JAIL="host",
                   JOBBOARD_FAKE="1", PYTHONDONTWRITEBYTECODE="1")
        spec = {"folder": folder, "scenario": scenario, "retry_scenario": retry}
        r = subprocess.run([sys.executable, "-c", DRIVER, json.dumps(spec)], cwd=str(ROOT),
                           env=env, capture_output=True, text=True, timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT=")][-1]
        out = json.loads(line[7:])
        self.ids, self.queued = out["ids"], out["queued"]
        return out

    def meta(self, jid):
        return json.loads((self.jobs / jid / "meta.json").read_text(encoding="utf-8"))

    def test_a_canned_plan_lands_in_the_folder_and_on_the_board(self):
        self.run_plan([{"write": "ROADMAP.md", "content": "# M1 - it runs\nYou can save a note.\n"},
                       {"write": "tasks.json", "content": PLAN_TEXT},
                       {"say": "The plan is written."}])
        self.assertEqual(len(self.ids), 1)
        tasks, errs = planning.read_tasks(self.work / "my-app")
        self.assertEqual(errs, [])
        self.assertEqual([t["id"] for t in tasks], ["t1", "t2", "t3"])
        rec = planning.load(self.plans, "my-app")
        self.assertEqual((rec["plan_status"], rec["plan_job"]), ("draft", self.ids[0]))
        m = self.meta(self.ids[0])
        self.assertEqual((m["plan_job"], m["verify_off"]), (1, True))   # a plan, not a build
        self.assertTrue(any("Plan ready" in e["text"] for e in timeline(self.jobs / self.ids[0])))
        # and the board still knows it is only a plan
        self.assertIn("Review the plan", planning.job_card_html(m, self.plans))

    def test_a_refused_plan_is_handed_back_once_and_then_stops(self):
        bad = '[{"id": "t1", "milestone": "M1", "title": "one", "prompt": "p"},\n' \
              ' {"id": "t1", "milestone": "M1", "title": "one again", "prompt": "q", ' \
              '"acceptance": ["ok"]}]'
        self.run_plan([{"write": "tasks.json", "content": bad}, {"say": "done"}],
                      retry=[{"write": "tasks.json", "content": bad}, {"say": "done again"}])
        self.assertEqual(len(self.ids), 2)
        self.assertEqual(self.queued, [])
        first, second = self.meta(self.ids[0]), self.meta(self.ids[1])
        self.assertEqual(second["plan_retry"], 1)
        self.assertEqual(second["parent"], self.ids[0])
        self.assertEqual(second["mode"], first["mode"])
        prompt = (self.plans / "retry-prompt.txt").read_text(encoding="utf-8")
        self.assertIn("was refused", prompt)
        self.assertIn("duplicate id 't1'", prompt)
        self.assertIn("Do not write any app code", prompt)
        self.assertEqual(planning.load(self.plans, "my-app")["plan_status"], "invalid")


# ------------------------------------------------------------------ the pages

class Pages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="jb04a-web-"))
        cls.saved = (jobboard.JOBS, jobboard.WORK, jobboard.PLANS, jobboard.PORT, jobboard.READONLY)
        jobboard.JOBS = cls.tmp / "jobs"
        jobboard.WORK = cls.tmp / "work"
        jobboard.PLANS = cls.tmp / "plans"
        for d in (jobboard.JOBS, jobboard.WORK, jobboard.PLANS):
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
        jobboard.JOBS, jobboard.WORK, jobboard.PLANS, jobboard.PORT, jobboard.READONLY = cls.saved

    def setUp(self):
        self.folder = "p" + re.sub(r"[^a-z0-9]", "", self.id().split(".")[-1].lower())[:40]
        (jobboard.WORK / self.folder).mkdir(parents=True, exist_ok=True)
        planning.save(jobboard.PLANS, planning.new_record(self.folder, "My Notes", "web",
                                                          "A notes app that works offline."))

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

    def plan_form(self, tasks, extra=None):
        fields = {}
        for i, t in enumerate(tasks, 1):
            fields[f"tid{i}"] = t["id"]
            fields[f"milestone{i}"] = t["milestone"]
            fields[f"title{i}"] = t["title"]
            fields[f"prompt{i}"] = t["prompt"]
            fields[f"acc{i}"] = "\n".join(t.get("acceptance") or [])
            fields[f"after{i}"] = ", ".join(t.get("depends_on") or [])
            fields[f"size{i}"] = t.get("size") or ""
            fields.update(extra or {})
        return fields

    def tasks_file(self):
        return json.loads((jobboard.WORK / self.folder / "tasks.json").read_text(encoding="utf-8"))

    # ---- the new project page

    def test_the_new_project_page_has_name_kind_outline_and_the_folder_choice(self):
        code, html, _ = self.fetch("/project/new")
        self.assertEqual(code, 200)
        for want in ('name="name"', 'name="kind"', 'name="outline"', 'name="folder"',
                     "Android app", "Windows app", "Web app", "Start a new folder"):
            self.assertIn(want, html)
        self.assertIn('href="/project/new"', html)        # it is reachable from every page

    def test_creating_a_project_makes_the_folder_on_its_own_branch(self):
        code, html, url = self.fetch("/project/new", {"name": "fresh-app", "kind": "web",
                                                      "outline": "A notes app for my phone."})
        self.assertEqual(code, 200)
        self.assertTrue(url.endswith("/project/fresh-app"), url)
        self.assertIn("No plan yet", html)
        root = jobboard.WORK / "fresh-app"
        self.assertEqual(git(root, "rev-parse", "--abbrev-ref", "HEAD").strip(), "agent/fresh-app")
        self.assertEqual(git(root, "log", "--pretty=%s").strip(), "start fresh-app")
        rec = planning.load(jobboard.PLANS, "fresh-app")
        self.assertEqual((rec["name"], rec["kind"]), ("fresh-app", "web"))
        self.assertIn("A notes app for my phone.", rec["outline"])

    def test_a_name_that_could_not_be_a_folder_is_refused(self):
        code, html, _ = self.fetch("/project/new", {"name": "my notes app", "kind": "web",
                                                    "outline": "notes"})
        self.assertEqual(code, 400)
        self.assertIn("simple name", html)
        self.assertFalse((jobboard.WORK / "my notes app").exists())

    def test_an_existing_folder_is_planned_in_without_making_a_new_one(self):
        (jobboard.WORK / "old-app").mkdir(exist_ok=True)
        code, _html, url = self.fetch("/project/new", {"name": "Old notes", "kind": "android",
                                                       "outline": "notes", "folder": "old-app"})
        self.assertEqual(code, 200)
        self.assertTrue(url.endswith("/project/old-app"), url)
        self.assertEqual(planning.load(jobboard.PLANS, "old-app")["name"], "Old notes")
        self.assertFalse((jobboard.WORK / "old-app" / ".git").exists())   # we did not touch it

    def test_a_folder_that_already_exists_is_not_overwritten(self):
        self.fetch("/project/new", {"name": "twice-app", "kind": "web", "outline": "notes"})
        code, html, _ = self.fetch("/project/new", {"name": "twice-app", "kind": "web", "outline": "more"})
        self.assertEqual(code, 400)
        self.assertIn("already exists", html)

    def test_a_folder_that_is_already_a_project_is_not_started_again(self):
        """Picking an existing folder is how you plan inside it - but a folder that already has a
        project record here must not have that record (outline, plan, approval) thrown away."""
        write_plan(jobboard.WORK / self.folder, GOOD_PLAN)
        planning.approve(jobboard.PLANS, planning.load(jobboard.PLANS, self.folder),
                         jobboard.WORK / self.folder, GOOD_PLAN)
        code, html, _ = self.fetch("/project/new", {"name": "the same thing", "kind": "web",
                                                    "folder": self.folder, "outline": "Something else."})
        self.assertEqual(code, 400)
        self.assertIn("is already a project here", html)
        rec = planning.load(jobboard.PLANS, self.folder)
        self.assertEqual(rec["name"], "My Notes")
        self.assertEqual(rec["outline"], "A notes app that works offline.")
        self.assertTrue(rec["approved_at"])            # the approved plan is still there

    def test_a_missing_outline_is_refused_with_what_was_typed_kept(self):
        code, html, _ = self.fetch("/project/new", {"name": "half-app", "kind": "web", "outline": ""})
        self.assertEqual(code, 400)
        self.assertIn('value="half-app"', html)
        self.assertIn("Paste the outline", html)

    # ---- the project page

    def test_make_a_plan_queues_a_job_and_runs_nothing(self):
        code, html, url = self.fetch(f"/project/edit/{self.folder}",
                                     {"outline": "Notes, offline.", "plan": "1", "mode": "local-qwen"})
        self.assertEqual(code, 200)
        jid = re.search(r"/job/(\d{8}-\d{6}-[0-9a-f]{4})$", url).group(1)
        m = json.loads((jobboard.JOBS / jid / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual((m["status"], m["folder"], m["plan_job"], m["verify_off"]),
                         ("queued", self.folder, 1, True))
        self.assertIn("Do not write any app code", m["job"])
        self.assertIn("Notes, offline.", m["job"])
        self.assertEqual(planning.load(jobboard.PLANS, self.folder)["plan_status"], "running")
        self.assertIn("The planner is working on it", self.fetch(f"/project/{self.folder}")[1])

    def test_a_plan_job_the_worker_refused_leaves_nothing_waiting(self):
        """The worker re-checks every queued record and may refuse one. The project must then say
        the plan was not made, not go on claiming "the planner is working on it"."""
        rec = planning.load(jobboard.PLANS, self.folder)
        rec["plan_status"] = "running"
        planning.save(jobboard.PLANS, rec)
        m = jobboard.new_job("local-qwen", self.folder, "Plan the app", False,
                             extra=dict(planning.plan_extra(), plan_retry=99))   # over 3: refused
        self.assertFalse(jobboard.process_job(m["id"]))
        done = jobboard.load_meta(m["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("Refused", done["fail_note"])
        self.assertNotEqual(planning.load(jobboard.PLANS, self.folder)["plan_status"], "running")
        html = self.fetch(f"/project/{self.folder}")[1]
        self.assertNotIn("The planner is working on it", html)
        self.assertIn("no tasks.json", html)
        mine = [mm for _, mm in jobboard.all_job_dirs() if mm["folder"] == self.folder]
        self.assertEqual([mm["status"] for mm in mine], ["failed"])   # nothing queued behind it

    def test_the_outline_is_saved_and_shown_again(self):
        code, html, url = self.fetch(f"/project/edit/{self.folder}", {"outline": "Notes with tags",
                                                                      "save": "1"})
        self.assertEqual(code, 200)
        self.assertTrue(url.endswith(f"/project/{self.folder}?saved=1"), url)
        self.assertEqual(planning.load(jobboard.PLANS, self.folder)["outline"], "Notes with tags")
        self.assertIn("Notes with tags", self.fetch(f"/project/{self.folder}")[1])

    def test_an_empty_outline_is_refused(self):
        code, html, _ = self.fetch(f"/project/edit/{self.folder}", {"outline": "  ", "save": "1"})
        self.assertEqual(code, 400)
        self.assertIn("cannot be empty", html)
        self.assertEqual(planning.load(jobboard.PLANS, self.folder)["outline"],
                         "A notes app that works offline.")

    def test_the_projects_page_lists_it_with_its_plan(self):
        code, html, _ = self.fetch("/projects")
        self.assertEqual(code, 200)
        self.assertIn(self.folder, html)
        self.assertIn("No plan yet", html)

    # ---- the plan review page

    def test_the_plan_page_shows_every_task_with_its_controls(self):
        write_plan(jobboard.WORK / self.folder, GOOD_PLAN)
        code, html, _ = self.fetch(f"/plan/{self.folder}")
        self.assertEqual(code, 200)
        self.assertIn('<h2>Plan for My Notes</h2>', html)
        for want in ('name="tid1"', 'name="tid3"', 'value="Task t2"', "Done when these are true",
                     "Approve plan", "Add task", "after: t2", "M2 - it remembers",
                     'name="move" value="t2:up"', 'name="drop" value="t2"'):
            self.assertIn(want, html)
        self.assertIn("Nothing runs until you approve it", html)

    def test_saving_writes_the_folder_s_tasks_file(self):
        write_plan(jobboard.WORK / self.folder, GOOD_PLAN)
        tasks = [dict(GOOD_PLAN[0], title="Set up the skeleton and the tests")] + GOOD_PLAN[1:]
        code, html, url = self.fetch(f"/plan/edit/{self.folder}", self.plan_form(tasks, {"save": "1"}))
        self.assertEqual(code, 200)
        self.assertTrue(url.endswith(f"/plan/{self.folder}?ok=1"), url)
        self.assertEqual(self.tasks_file()[0]["title"], "Set up the skeleton and the tests")
        self.assertIn("Plan saved.", html)
        self.assertNotIn("Plan saved.", self.fetch(f"/plan/{self.folder}")[1])   # the note is once

    def test_a_broken_plan_is_not_saved_and_the_page_says_why(self):
        write_plan(jobboard.WORK / self.folder, GOOD_PLAN)
        tasks = [dict(t, acceptance=[]) for t in GOOD_PLAN]
        code, html, _ = self.fetch(f"/plan/edit/{self.folder}", self.plan_form(tasks, {"save": "1"}))
        self.assertEqual(code, 400)
        self.assertIn("Not saved - fix these first", html)
        self.assertIn("acceptance", html)
        self.assertEqual(self.tasks_file()[0]["acceptance"], ["it works"])

    def test_a_plan_with_a_cycle_cannot_be_approved(self):
        a, b = T("ta", ["tb"]), T("tb", ["ta"])
        write_plan(jobboard.WORK / self.folder, [a, b])
        code, html, _ = self.fetch(f"/plan/edit/{self.folder}", self.plan_form([a, b], {"approve": "1"}))
        self.assertEqual(code, 400)
        self.assertIn("circular", html)
        self.assertIn("was not approved", html)
        self.assertIsNone(planning.load(jobboard.PLANS, self.folder)["approved_at"])

    def test_approving_a_good_plan_records_it_and_still_runs_nothing(self):
        write_plan(jobboard.WORK / self.folder, GOOD_PLAN)
        code, html, url = self.fetch(f"/plan/edit/{self.folder}", self.plan_form(GOOD_PLAN,
                                                                                {"approve": "1"}))
        self.assertEqual(code, 200)
        self.assertTrue(url.endswith(f"/plan/{self.folder}?ok=2"), url)
        rec = planning.load(jobboard.PLANS, self.folder)
        self.assertTrue(rec["approved_at"])
        self.assertEqual([t["id"] for t in rec["tasks"]], ["t1", "t2", "t3"])
        self.assertIn("Plan approved. Nothing runs until you start the tasks.", html)
        self.assertIn("Nothing runs until you start them",
                      self.fetch(f"/plan/{self.folder}")[1])

    def test_up_down_delete_and_add_change_the_page_but_not_the_file(self):
        write_plan(jobboard.WORK / self.folder, GOOD_PLAN)
        before = self.tasks_file()
        code, html, _ = self.fetch(f"/plan/edit/{self.folder}",
                                   self.plan_form(GOOD_PLAN, {"move": "t3:up"}))
        self.assertEqual(code, 200)
        self.assertIn('name="tid2" value="t3"', html)
        code, html, _ = self.fetch(f"/plan/edit/{self.folder}", self.plan_form(GOOD_PLAN, {"add": "1"}))
        self.assertEqual(code, 200)
        self.assertIn("Added t4", html)
        code, html, _ = self.fetch(f"/plan/edit/{self.folder}", self.plan_form(GOOD_PLAN, {"drop": "t2"}))
        self.assertEqual(code, 200)
        self.assertIn("Deleted t2", html)
        self.assertIn('name="tid2" value="t3"', html)
        self.assertEqual(self.tasks_file(), before)      # nothing was written yet

    def test_a_form_that_came_back_empty_changes_nothing(self):
        write_plan(jobboard.WORK / self.folder, GOOD_PLAN)
        code, html, _ = self.fetch(f"/plan/edit/{self.folder}", {"save": "1"})
        self.assertEqual(code, 400)
        self.assertIn("came back empty", html)
        self.assertEqual(len(self.tasks_file()), 3)

    def test_pages_need_the_board_cookie_and_unknown_projects_have_no_page(self):
        self.assertEqual(self.fetch(f"/plan/{self.folder}", cookie=False)[0], 403)
        self.assertEqual(self.fetch("/project/no-such-thing")[0], 404)
        self.assertEqual(self.fetch("/plan/no-such-thing")[0], 404)
        self.assertEqual(self.fetch("/plan/edit/no-such-thing", {"save": "1"})[0], 404)
        self.assertEqual(self.fetch("/project/..%2Fetc")[0], 404)


if __name__ == "__main__":
    unittest.main()
