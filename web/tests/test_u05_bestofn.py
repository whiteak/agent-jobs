#!/usr/bin/env python3
r"""Upgrade 5: best-of-N for hard steps.

* Pieces  - names, settings keys, the plan and the form fields, the scores (one case per
            tie-break of the given bestofn ranking), prune_old with a fake clock, and the
            Attempts card HTML.
* Git     - the plumbing against a REAL temp repo, SafeGit with no wrapper (host mode):
            the clone gets its own branch, leftover edits are committed, the branch comes
            back through a bundle, the winner is checked out as agent/<job id>, and a
            leftover uncommitted state in the project folder is reported, not bulldozed.
* Jobs    - fake-agent runs in a child process (JOBBOARD_JAIL=host + JOBBOARD_FAKE=1), a
            different scenario per attempt: a hard job runs all its tries and keeps the
            best, an unmarked job stops after a clean first try, every try can fail,
            "Use this one instead" switches the folder, and auto_attempts queues a fresh
            best-of-N run on its own.
* Pages   - the real handler served in-process on its own port (jobboard.PORT set): the
            card on the job page, POST /use-attempt and /try-hard, the hard-job fields and
            their validation on /new.

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
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

if "jobboard" not in sys.modules:      # standalone run: same isolated env as test_jobstatus
    _hb = tempfile.mkdtemp(prefix="jb05-home-")
    os.environ.update({"HOME": _hb, "JOBBOARD_JOBS": os.path.join(_hb, "jobs"),
                       "JOBBOARD_WORK": os.path.join(_hb, "work"), "JOBBOARD_READONLY": "1"})
import jobboard  # noqa: E402
import upgrades  # noqa: E402
import attempts  # noqa: E402
import badges    # noqa: E402
from agentjobs_upgrades import bestofn  # noqa: E402
from agentjobs_upgrades.safegit import GitError  # noqa: E402

GOOD = "def add(a, b):\n    return int(a + b)\n"
BAD = "def add(a, b):\n    return a - b - 0\n"
TEST = ("import sys, os, unittest\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
        "    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n")


def git(repo, *a, env=None):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t",
                           "-c", "user.email=t@t", *a], cwd=str(repo), check=True,
                          capture_output=True, text=True, env=env).stdout


def make_project(tmp: Path, passing=False) -> Path:
    p = Path(tmp) / "proj"
    (p / "tests").mkdir(parents=True)
    (p / "calc.py").write_text(GOOD if passing else BAD)
    (p / "tests" / "test_calc.py").write_text(TEST)
    (p / "AGENT_CHECKS.json").write_text(json.dumps({"checks": [
        {"name": "tests", "cmd": f"{sys.executable} -m unittest discover -s tests -p 'test_*.py'",
         "timeout_s": 60}]}))
    (p / "PLAN.md").write_text("# Plan\n- [ ] step one\n- [ ] step two\n")
    (p / ".gitignore").write_text("__pycache__/\n")
    git(p, "init", "-q", "-b", "main")
    git(p, "add", "-A")
    git(p, "commit", "-qm", "base")
    return p


# ------------------------------------------------------------------ names, settings, plan

class Pieces(unittest.TestCase):
    def test_names_are_safe_and_recognisable(self):
        jid = "20261111-120000-ab12"
        self.assertEqual(attempts.attempt_folder("my-app", jid, 2), "my-app-try2-ab12")
        long_name = attempts.attempt_folder("x" * 60, jid, 1)
        self.assertTrue(long_name.startswith("x" * 40) and long_name.endswith("-try1-ab12"))
        self.assertLessEqual(len(long_name), 64)             # still a legal ~/agent-work folder name
        self.assertTrue(jobboard.FOLDER_RE.fullmatch(long_name))
        self.assertEqual(attempts.attempt_branch(jid, 3), "agent/20261111-120000-ab12-try3")
        self.assertEqual(attempts.job_branch(jid), "agent/20261111-120000-ab12")
        self.assertTrue(jobboard.attempts is attempts)
        self.assertTrue(attempts.ATTEMPT_TAIL.search("my-app-try2-ab12"))
        self.assertFalse(attempts.ATTEMPT_TAIL.search("my-app"))
        self.assertTrue(attempts.is_attempt_folder("proj-try1-ab12"))
        self.assertFalse(attempts.is_attempt_folder("proj-try1-zzzz"))  # someone else's folder
        self.assertFalse(attempts.is_attempt_folder("proj-try1"))

    def test_attempt_folders_stay_out_of_the_folder_picker(self):
        t = Path(tempfile.mkdtemp())
        for name in ("proj", "proj-try1-ab12", "other-try2-9f3c", "plain-folder", "proj-try1-zzzz"):
            (t / name).mkdir()
        with mock.patch.object(jobboard, "WORK", t):
            self.assertEqual(jobboard.folders(), ["plain-folder", "proj", "proj-try1-zzzz"])

    def test_settings_keep_the_new_keys(self):
        t = Path(tempfile.mkdtemp()) / "s.json"
        cfg = upgrades.load_settings(t)
        self.assertEqual((cfg["attempts"], cfg["stop_early"], cfg["auto_attempts"]), (3, True, False))
        t.write_text(json.dumps({"attempts": 4, "stop_early": False, "auto_attempts": True,
                                 "hard": "yes"}))
        cfg = upgrades.load_settings(t)
        self.assertEqual((cfg["attempts"], cfg["stop_early"], cfg["auto_attempts"]), (4, False, True))
        self.assertNotIn("hard", cfg)                       # not a setting, and a string never gets in

    def test_plan_from_the_record(self):
        self.assertIsNone(attempts.plan({}, {}))
        self.assertIsNone(attempts.plan({"attempts": 0}, {}))
        self.assertIsNone(attempts.plan({"attempts": 1}, {}))       # 2-4 only
        self.assertIsNone(attempts.plan({"attempts": 5}, {}))
        self.assertIsNone(attempts.plan({"attempts": True}, {}))    # not an int
        self.assertEqual(attempts.plan({"attempts": 3, "hard": True}, {}),
                         {"n": 3, "hard": True, "stop_early": True})
        self.assertEqual(attempts.plan({"attempts": 2}, {"stop_early": False})["stop_early"], False)
        self.assertEqual(attempts.plan({"attempts": 2, "stop_early": True}, {"stop_early": False})["stop_early"], True)

    def test_phase_tag_names_the_try_being_checked(self):
        self.assertEqual(attempts.phase_tag({"attempts_total": 3, "attempts_current": 2}),
                         "Attempt 2 of 3 - ")
        for m in ({}, {"attempts_total": 3}, {"attempts_current": 2},
                  {"attempts_total": 3, "attempts_current": 0},
                  {"attempts_total": 3, "attempts_current": 4},
                  {"attempts_total": True, "attempts_current": 1}):
            self.assertEqual(attempts.phase_tag(m), "")

    def test_record_fields_are_validated_before_a_job_runs(self):
        base = {"id": "20261111-120000-ab12", "mode": "local-qwen", "folder": "proj", "job": "x",
                "offline": False}
        for bad in ({"attempts": 9}, {"attempts": "3"}, {"attempts": True}, {"hard": "yes"},
                    {"stop_early": 1}):
            self.assertEqual(jobboard.check_record(base["id"], dict(base, **bad)),
                             next(iter(bad)), bad)
        for ok in ({"attempts": 4, "hard": True}, {"attempts": 2, "stop_early": False}):
            self.assertIsNone(jobboard.check_record(base["id"], dict(base, **ok)), ok)

    def test_form_fields_and_their_validation(self):
        html = attempts.new_job_fields({"attempts": 3})
        self.assertIn('name="hard"', html)
        self.assertIn("Hard job: try it 3 times", html)
        self.assertIn("about 3&times; longer", html)
        self.assertEqual(attempts.form_extra({}, {}), {})
        self.assertEqual(attempts.form_extra({"hard": ["on"]}, {"attempts": 3}),
                         {"attempts": 3, "hard": True})
        self.assertEqual(attempts.form_extra({"hard": ["on"], "attempts": ["4"]}, {}),
                         {"attempts": 4, "hard": True})
        self.assertIsNone(attempts.form_extra({"hard": ["on"], "attempts": ["9"]}, {}))
        self.assertIsNone(attempts.form_extra({"hard": ["on"], "attempts": ["lots"]}, {}))

    def test_retry_carries_the_job_s_rules_and_stops_early(self):
        m = {"attempts": 6, "checks": [{"name": "t", "cmd": "x"}], "checks_source": "the new-job form",
             "verify_off": False, "fix_rounds": 2}
        extra = attempts.retry_extra(m, {"attempts": 3})
        self.assertEqual(extra["attempts"], 4)               # clamped to 2-4
        self.assertFalse(extra["hard"])                      # not marked hard: try 1 can be enough
        self.assertEqual(extra["checks"], m["checks"])
        self.assertEqual(extra["fix_rounds"], 2)
        self.assertEqual(attempts.retry_extra({}, {"attempts": 2})["attempts"], 2)

    def test_fake_mode_can_give_every_attempt_its_own_scenario(self):
        m = {"mode": "fake", "job": json.dumps({"attempts": [[{"say": "one"}], [{"say": "two"}]]})}
        self.assertEqual(json.loads(attempts.attempt_job(m, 1)), [{"say": "one"}])
        self.assertEqual(json.loads(attempts.attempt_job(m, 2)), [{"say": "two"}])
        self.assertEqual(json.loads(attempts.attempt_job(m, 12)), [{"say": "two"}])   # runs out: last one
        plain = {"mode": "fake", "job": json.dumps([{"say": "same"}])}
        self.assertEqual(json.loads(attempts.attempt_job(plain, 2)), [{"say": "same"}])
        self.assertEqual(attempts.attempt_job({"mode": "local-qwen", "job": "do it"}, 1), "do it")

    # ---- scores

    def test_status_of_an_attempt(self):
        ok = {"status": "done", "verify": {"status": "passed"}}
        self.assertEqual(attempts.status_of(ok), "passed")
        self.assertEqual(attempts.status_of({"status": "failed", "verify": {"status": "failed"}}), "failed")
        self.assertEqual(attempts.status_of({"status": "safety_stop"}), "safety_stop")
        self.assertEqual(attempts.status_of({"status": "stopped", "verify": {"status": "passed"}}), "stopped")
        self.assertEqual(attempts.status_of({"status": "done"}), "unverified")
        self.assertEqual(attempts.status_of({}), "unverified")

    def test_tests_passed_comes_from_the_checks(self):
        m = {"verify": {"final": {"results": [{"passed": 41, "failed": 1}, {"passed": 3}]}}}
        self.assertEqual(attempts.tests_passed(m), 44)
        self.assertEqual(attempts.tests_passed({}), 0)

    def test_attempt_of_feeds_the_given_ranking(self):
        a = attempts.attempt_of({"k": 2, "branch": "agent/x-try2", "status": "passed",
                                 "tests_passed": 7, "test_files_changed": 1, "diff_lines": 30,
                                 "seconds": 12.5, "issues": []})
        self.assertEqual((a.k, a.status, a.tests_passed, a.diff_lines, a.seconds), (2, "passed", 7, 30, 12.5))
        self.assertTrue(a.clean_pass)
        flagged = attempts.attempt_of({"k": 1, "status": "passed", "issues": ["Test file deleted"]})
        self.assertFalse(flagged.clean_pass)

    def _rec(self, k, **kw):
        r = {"k": k, "branch": f"agent/j-try{k}", "status": "passed", "tests_passed": 2,
             "test_files_changed": 0, "diff_lines": 5, "seconds": 10.0 + k}
        r.update(kw)
        return attempts.attempt_of(r)

    def test_every_tie_break_in_order(self):
        def win(*recs):
            w, ranked = bestofn.pick_winner(list(recs))
            return w, bestofn.why(w, ranked), [a.k for a in ranked]
        # (a) a clean pass beats everything, even a bigger change
        w, why, _ = win(self._rec(1, status="failed", tests_passed=99), self._rec(2, diff_lines=900))
        self.assertEqual((w.k, why), (2, "Attempt 2 was the only one that passed cleanly."))
        # (b) most tests passing
        w, why, _ = win(self._rec(1, tests_passed=10), self._rec(2, tests_passed=12))
        self.assertEqual((w.k, why), (2, "Attempt 2 won: more tests passing."))
        # (c) fewest test files changed
        w, why, _ = win(self._rec(1, tests_passed=10, test_files_changed=3),
                        self._rec(2, tests_passed=10, test_files_changed=1))
        self.assertEqual((w.k, why), (2, "Attempt 2 won: fewer test files changed."))
        # (d) smallest change
        w, why, _ = win(self._rec(1, tests_passed=10, diff_lines=400),
                        self._rec(2, tests_passed=10, diff_lines=12))
        self.assertEqual((w.k, why), (2, "Attempt 2 won: a smaller change."))
        # (e) fastest
        w, why, _ = win(self._rec(1, tests_passed=10, diff_lines=12, seconds=40.0),
                        self._rec(2, tests_passed=10, diff_lines=12, seconds=11.0))
        self.assertEqual((w.k, why), (2, "Attempt 2 won: finished faster."))
        # a needs-review pass is not a clean pass, so nobody wins but they are still ranked
        w, why, ranked = win(self._rec(1, issues=["Test file deleted: tests/a.py"]),
                             self._rec(2, status="needs_review"))
        self.assertIsNone(w)
        self.assertEqual(why, "No attempt passed its checks.")
        self.assertEqual(ranked, [1, 2])

    def test_stop_early_rules(self):
        clean = self._rec(1)
        self.assertFalse(bestofn.should_continue([clean], 3, hard=False, stop_early=True))
        self.assertTrue(bestofn.should_continue([clean], 3, hard=True, stop_early=True))
        self.assertTrue(bestofn.should_continue([clean], 3, hard=False, stop_early=False))
        self.assertTrue(bestofn.should_continue([self._rec(1, status="failed")], 3, False, True))
        self.assertFalse(bestofn.should_continue([clean, self._rec(2), self._rec(3)], 3, True, True))
        self.assertFalse(bestofn.should_continue([self._rec(1, status="safety_stop")], 3, True, True))

    def test_prune_old_only_removes_attempt_branches_past_their_7_days(self):
        repo = Path(tempfile.mkdtemp()) / "r"
        repo.mkdir()
        old = dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T00:00:00 +0000",
                   GIT_COMMITTER_DATE="2026-01-01T00:00:00 +0000")
        git(repo, "init", "-q", "-b", "main")
        (repo / "a").write_text("1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "old", env=old)         # committed a long time ago
        git(repo, "update-ref", "refs/heads/agent/j-old-try1", git(repo, "rev-parse", "HEAD").strip())
        (repo / "b").write_text("2\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "recent")               # committed now
        head = git(repo, "rev-parse", "HEAD").strip()
        for ref in ("agent/j-new-try2", "agent/j-main", "agent/j-discarded-1"):
            git(repo, "update-ref", f"refs/heads/{ref}", head)
        g = attempts._jail_git(repo, "r", "j")
        with mock.patch.object(upgrades, "HOST_MODE", True):
            removed = attempts.prune_old(g, days=7)
            again = attempts.prune_old(g, days=7)
        self.assertEqual(removed, ["refs/heads/agent/j-old-try1"])
        self.assertEqual(again, [])
        left = git(repo, "branch", "--list", "agent/*")
        for keep in ("agent/j-new-try2", "agent/j-main", "agent/j-discarded-1"):
            self.assertIn(keep, left)
        self.assertNotIn("agent/j-old-try1", left)

    def test_card_shows_every_try_and_its_numbers(self):
        m = {"id": "20261111-120000-ab12", "status": "done", "attempts_total": 3,
             "attempt_log": [
                 {"k": 1, "branch": "agent/x-try1", "status": "failed", "tests_passed": 1,
                  "diff_lines": 4, "files": [["calc.py", 2, 2]], "seconds": 30, "commit": "abc"},
                 {"k": 2, "branch": "agent/x-try2", "status": "passed", "tests_passed": 2,
                  "diff_lines": 1, "files": [["calc.py", 1, 1], ["tests/test_a.py", 3, 0]],
                  "seconds": 45, "commit": "def"}],
             "attempts_final": {"winner": 2, "why": "Attempt 2 won: a smaller change."}}
        html = attempts.card_html(m, m["id"], True, {"attempts": 3})
        self.assertIn("<h2>Attempts</h2>", html)
        self.assertIn("Attempt 2 won: a smaller change.", html)
        self.assertIn("checks failed", html)
        self.assertIn("2 passing", html)
        self.assertIn("agent/x-try1", html)
        self.assertIn('action="/use-attempt/20261111-120000-ab12"', html)
        self.assertEqual(html.count("Use this one instead"), 1)      # the kept one has no button
        self.assertIn("1 file, 4 lines", html)
        self.assertIn("2 files, 1 line", html)
        self.assertIn("for 7 days", html)
        self.assertNotIn("Try 3 fresh attempts", html)               # it already tried N times
        self.assertEqual(attempts.card_html({"id": "x", "status": "done"}, "x", True), "")

    def test_card_while_running_shows_the_progress_and_the_cost(self):
        m = {"id": "j", "status": "running", "attempts": 3, "attempts_total": 3, "attempts_current": 2,
             "attempt_log": [{"k": 1, "branch": "agent/j-try1", "status": "failed", "commit": "a",
                              "tests_passed": 0, "diff_lines": 0, "files": [], "seconds": 5}]}
        html = attempts.card_html(m, "j", False, {"attempts": 3})
        self.assertIn("Attempt 2 of 3", html)
        self.assertIn("about 3&times; longer", html)

    def test_card_offers_fresh_attempts_for_a_job_that_failed_after_fix_rounds(self):
        m = {"id": "j", "status": "failed", "verify": {"status": "failed",
                                                      "banner": {"colour": "red", "text": "Failed after 3 fix rounds"}}}
        html = attempts.card_html(m, "j", True, {"attempts": 4})
        self.assertIn('action="/try-hard/j"', html)
        self.assertIn("Try 4 fresh attempts", html)
        self.assertIn("about 4&times; longer", html)
        self.assertNotIn('action="/try-hard/j"', attempts.card_html(m, "j", False, {}))  # read-only: no button


# ------------------------------------------------------------------ the git plumbing

class Git(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="u05-git-"))
        self.work = self.t / "work"
        self.repo = make_project(self.work)
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        self.m = {"id": "20261111-120000-ab12", "folder": "proj", "job": "fix calc"}
        self.host = mock.patch.object(upgrades, "HOST_MODE", True)
        self.host.start()
        self.addCleanup(self.host.stop)

    def folders(self):
        return sorted(d.name for d in self.work.iterdir() if d.is_dir())

    def test_a_try_gets_its_own_repo_on_its_own_branch(self):
        folder, g = attempts.prepare(self.m, 1, self.work, self.base)
        self.assertEqual(folder, "proj-try1-ab12")
        self.assertEqual(g.branch(), "agent/20261111-120000-ab12-try1")
        self.assertEqual(g.head(), self.base)
        self.assertTrue((self.work / folder / ".git").is_dir(),
                        "a full clone: a worktree's git dir would be invisible inside the jail")
        self.assertEqual((self.work / folder / "calc.py").read_text(), BAD)   # the base commit, as it was

    def test_attempts_never_see_each_other(self):
        f1, g1 = attempts.prepare(self.m, 1, self.work, self.base)
        f2, g2 = attempts.prepare(self.m, 2, self.work, self.base)
        (self.work / f1 / "only1.py").write_text("1\n")
        self.assertFalse((self.work / f2 / "only1.py").exists())
        self.assertEqual(g2.head(), self.base)
        self.assertEqual(sorted(self.folders()), ["proj", "proj-try1-ab12", "proj-try2-ab12"])

    def test_leftovers_are_committed_and_packed_into_a_bundle(self):
        folder, g = attempts.prepare(self.m, 1, self.work, self.base)
        (self.work / folder / "calc.py").write_text(GOOD)          # the agent never committed
        (self.work / folder / "tests" / "test_calc.py").write_text(TEST + "\n")
        extra = attempts.collect(self.m, 1, g, self.base)
        self.assertEqual(extra["test_files_changed"], 1)
        self.assertEqual(extra["diff_lines"], 3)
        self.assertEqual(sorted(f[0] for f in extra["files"]), ["calc.py", "tests/test_calc.py"])
        self.assertIn("what was left in the folder", git(self.work / folder, "log", "--oneline"))
        self.assertTrue(Path(extra["bundle"]).is_file())
        attempts.bring_home(self.m, self.work, extra["bundle"], attempts.attempt_branch(self.m["id"], 1))
        self.assertIn("agent/20261111-120000-ab12-try1", git(self.repo, "branch", "--list", "agent/*"))
        self.assertEqual(git(self.repo, "rev-parse", "agent/20261111-120000-ab12-try1").strip(),
                         extra["commit"])

    def test_the_winner_lands_on_the_job_branch_in_the_project_folder(self):
        folder, g = attempts.prepare(self.m, 1, self.work, self.base)
        (self.work / folder / "calc.py").write_text(GOOD)
        extra = attempts.collect(self.m, 1, g, self.base)
        attempts.bring_home(self.m, self.work, extra["bundle"], attempts.attempt_branch(self.m["id"], 1))
        self.assertEqual(attempts.apply_winner(self.m, self.work, attempts.attempt_branch(self.m["id"], 1)), "")
        self.assertEqual((self.repo / "calc.py").read_text(), GOOD)
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip(),
                         "agent/20261111-120000-ab12")
        self.assertEqual(git(self.repo, "rev-parse", "agent/20261111-120000-ab12").strip(), extra["commit"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD~1").strip(), self.base)  # same starting point

    def test_a_dirty_project_folder_is_reported_not_overwritten(self):
        (self.repo / "calc.py").write_text("# someone is editing this\n")   # uncommitted, tracked
        folder, g = attempts.prepare(self.m, 1, self.work, self.base)
        (self.work / folder / "calc.py").write_text(GOOD)
        extra = attempts.collect(self.m, 1, g, self.base)
        attempts.bring_home(self.m, self.work, extra["bundle"],
                            attempts.attempt_branch(self.m["id"], 1), extra["commit"])
        err = attempts.apply_winner(self.m, self.work, attempts.attempt_branch(self.m["id"], 1))
        self.assertIn("Could not put", err)
        self.assertIn(f"agent/{self.m['id']}-try1", err)
        self.assertEqual((self.repo / "calc.py").read_text(), "# someone is editing this\n")

    def test_a_try_that_changed_nothing_still_gets_its_branch_back(self):
        folder, g = attempts.prepare(self.m, 2, self.work, self.base)
        extra = attempts.collect(self.m, 2, g, self.base)
        self.assertIsNone(extra["bundle"])                    # an empty bundle is refused by git
        self.assertEqual(extra["commit"], self.base)
        attempts.bring_home(self.m, self.work, extra["bundle"],
                            attempts.attempt_branch(self.m["id"], 2), extra["commit"])
        self.assertEqual(git(self.repo, "rev-parse", f"agent/{self.m['id']}-try2").strip(), self.base)

    def test_changes_of_a_try_are_listed_with_their_sizes(self):
        folder, g = attempts.prepare(self.m, 1, self.work, self.base)
        (self.work / folder / "calc.py").write_text(GOOD + "\n# a comment\n")
        (self.work / folder / "tests" / "test_extra.py").write_text("import unittest\n")
        (self.work / folder / "new").mkdir(parents=True)
        (self.work / folder / "new" / "deep.py").write_text("x = 1\n")
        extra = attempts.collect(self.m, 1, g, self.base)
        by = {f[0]: f for f in extra["files"]}
        self.assertEqual(sorted(by), ["calc.py", "new/deep.py", "tests/test_extra.py"])
        self.assertEqual(extra["test_files_changed"], 1)
        self.assertEqual(extra["diff_lines"], by["calc.py"][1] + by["calc.py"][2]
                         + by["new/deep.py"][1] + by["tests/test_extra.py"][1])


# ------------------------------------------------------------------ fake-agent jobs

DRIVER = textwrap.dedent("""
    import json, sys
    import jobboard
    spec = json.loads(sys.argv[1])
    m = jobboard.new_job("fake", spec["folder"], json.dumps(spec["scenario"]), False, extra=spec.get("extra"))
    jobboard.process_job(m["id"])
    out = []
    if spec.get("then"):
        kind, args = spec["then"]
        if kind == "use-attempt":
            jobboard.u05_button(m["id"], lambda k: str(args.get(k, "")))
        else:
            jobboard.u01_button(kind, m["id"], lambda k: str(args.get(k, "")))
    if spec.get("retry"):
        again = json.loads((jobboard.JOBS / m["id"] / "meta.json").read_text())
        out.append("CHILD=" + jobboard.hard_retry(again)["id"])
    for line in out:
        print(line)
    print("JOBID=" + m["id"])
""")


class Jobs(unittest.TestCase):
    """Best-of-N runs through process_job in a child process (host jail + the fake agent)."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="u05-jobs-"))
        self.home = self.t / "home"
        (self.home / "agent-sandbox" / "state").mkdir(parents=True)
        self.work = self.t / "work"
        self.jobs = self.t / "jobs"
        self.work.mkdir()
        self.jobs.mkdir()
        self.settings = self.t / "settings.json"
        self.settings.write_text(json.dumps({"watchdog_tick_s": 1, "after_result_grace_s": 3}))

    def project(self, passing=False, name="proj"):
        return make_project(self.work, passing)

    def run_job(self, scenario, extra=None, folder="proj", then=None, retry=False, timeout=300):
        # JOBBOARD_READONLY=0: running this file on its own puts that flag in our own
        # environment (see the module header) and the child would inherit it - a
        # read-only board never starts the automatic fresh attempts.
        env = dict(os.environ, HOME=str(self.home), JOBBOARD_JOBS=str(self.jobs), JOBBOARD_WORK=str(self.work),
                   JOBBOARD_SETTINGS=str(self.settings), JOBBOARD_JAIL="host", JOBBOARD_FAKE="1",
                   JOBBOARD_READONLY="0", PYTHONDONTWRITEBYTECODE="1")
        spec = {"folder": folder, "scenario": scenario,
                "extra": extra if extra is not None else {"fix_rounds": 0},
                "then": then, "retry": retry}
        r = subprocess.run([sys.executable, "-c", DRIVER, json.dumps(spec)], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.jid = [l for l in r.stdout.splitlines() if l.startswith("JOBID=")][-1][6:]
        self.child = [l[6:] for l in r.stdout.splitlines() if l.startswith("CHILD=")]
        return json.loads((self.jobs / self.jid / "meta.json").read_text())

    def timeline(self):
        p = self.jobs / self.jid / "timeline.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def branches(self, repo):
        return re.findall(r"agent/\S+", git(repo, "branch", "--list", "agent/*"))

    def test_a_hard_job_runs_every_try_and_keeps_the_best(self):
        repo = self.project()
        big = GOOD + "\n# long note about how it was fixed\n" + "x = 1\ny = 2\n"
        scen = {"attempts": [[{"say": "I could not work it out"}],                       # try 1: fails
                             {"rounds": [[{"say": "I think that works"}],                # try 2: needs a fix round
                                         [{"write": "calc.py", "content": big},
                                          {"write": "NOTES.md", "content": "notes\n"}]]},
                             [{"write": "calc.py", "content": GOOD}]]}                  # try 3: works, smallest
        m = self.run_job(scen, extra={"fix_rounds": 1, "attempts": 3, "hard": True})
        log = m["attempt_log"]
        self.assertEqual([a["k"] for a in log], [1, 2, 3])               # a hard job never stops early
        self.assertEqual([a["status"] for a in log], ["failed", "passed", "passed"])
        self.assertLess(log[0]["tests_passed"], 2)                       # it broke one of the two tests
        self.assertEqual([a["tests_passed"] for a in log[1:]], [2, 2])
        self.assertGreater(log[1]["diff_lines"], log[2]["diff_lines"])
        self.assertEqual(m["status"], "done")
        self.assertEqual(m["attempts_final"]["winner"], 3)
        self.assertIn("smaller change", m["attempts_final"]["why"])
        self.assertEqual(m["verify"]["status"], "passed")               # the winner's checks are the job's
        # the winner's work is in the project folder, on the job's own branch
        self.assertEqual((repo / "calc.py").read_text(), GOOD)
        self.assertFalse((repo / "NOTES.md").exists())
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(), f"agent/{self.jid}")
        self.assertIn(f"agent/{self.jid}-try1", self.branches(repo))
        self.assertIn(f"agent/{self.jid}-try3", self.branches(repo))
        # the tries' folders are gone, their check logs are apart from each other, and a try
        # gets the usual fix rounds (try 2 needed one)
        self.assertEqual(sorted(d.name for d in self.work.iterdir() if d.is_dir()), ["proj"])
        for k in (1, 2, 3):
            self.assertTrue((self.jobs / self.jid / "attempts" / f"try-{k}" / "checks" / "round-0").is_dir())
        self.assertTrue((self.jobs / self.jid / "attempts" / "try-2" / "checks" / "round-1").is_dir())
        self.assertFalse((self.jobs / self.jid / "checks").exists())
        # the check badges of the job point at the winner's own log (U07b)
        last = len((m["verify"].get("rounds") or [{}])) - 1
        txt, _cut = badges.check_text(m, f"round{last}", 0, self.jobs / self.jid)
        self.assertIn("Ran 2 tests", txt)
        # and the page tells the story
        tl = [e["text"] for e in self.timeline()]
        self.assertIn("Attempt 3 of 3 started", tl)
        self.assertIn("Attempt 2 of 3: passed - 2 tests passing", tl)
        self.assertIn("All 3 attempts tried", tl)      # they ran out, nothing was skipped
        self.assertNotIn("No more attempts: attempt 3 passed cleanly", tl)

    def test_early_stop_after_one_clean_try_when_the_job_is_not_hard(self):
        self.project()
        scen = {"attempts": [[{"write": "calc.py", "content": GOOD}], [{"write": "extra.py", "content": "1\n"}]]}
        m = self.run_job(scen, extra={"fix_rounds": 0, "attempts": 3, "hard": False, "stop_early": True})
        self.assertEqual([a["k"] for a in m["attempt_log"]], [1])
        self.assertEqual(m["status"], "done")
        self.assertEqual(m["attempts_final"]["winner"], 1)
        self.assertEqual(m["attempts_final"]["why"], "Attempt 1 passed.")
        self.assertIn("No more attempts: attempt 1 passed cleanly", [e["text"] for e in self.timeline()])
        br = self.branches(self.work / "proj")
        self.assertIn(f"agent/{self.jid}-try1", br)
        self.assertNotIn(f"agent/{self.jid}-try2", br)
        self.assertEqual((self.work / "proj" / "extra.py").exists(), False)
        self.assertEqual(sorted(d.name for d in self.work.iterdir() if d.is_dir()), ["proj"])

    def test_when_every_try_fails_nothing_is_put_in_the_folder(self):
        repo = self.project()
        scen = {"attempts": [[{"say": "nope"}],
                             [{"write": "half.py", "content": "print('close')\n"}],
                             [{"write": "wide.py", "content": "a\n" * 40}]]}
        m = self.run_job(scen, extra={"fix_rounds": 0, "attempts": 3, "hard": True})
        self.assertEqual([a["status"] for a in m["attempt_log"]], ["failed"] * 3)
        self.assertEqual(m["status"], "failed")
        self.assertEqual(m["attempts_final"]["winner"], None)
        self.assertEqual(m["fail_note"], "No attempt passed its checks.")
        self.assertEqual(m["verify"]["status"], "failed")
        self.assertEqual(m["attempts_final"]["ranked"], [1, 2, 3])  # smallest change first
        br = self.branches(repo)
        for k in (1, 2, 3):
            self.assertIn(f"agent/{self.jid}-try{k}", br)      # all three stay for 7 days
        self.assertEqual((repo / "calc.py").read_text(), BAD)  # the folder is left exactly as it was
        self.assertFalse((repo / "wide.py").exists())
        self.assertFalse((repo / "half.py").exists())
        self.assertIn("Nothing was put in the folder", m["attempts_note"])
        self.assertEqual(sorted(d.name for d in self.work.iterdir() if d.is_dir()), ["proj"])

    def test_use_this_one_instead_moves_the_folder_to_another_try(self):
        repo = self.project()
        scen = {"attempts": [[{"write": "calc.py", "content": GOOD + "# try one\n"},
                              {"write": "one.py", "content": "1\n"}],
                             [{"write": "calc.py", "content": GOOD + "# try two\n"}]]}
        m = self.run_job(scen, extra={"fix_rounds": 0, "attempts": 2, "hard": True},
                         then=("use-attempt", {"k": 1}))
        m = json.loads((self.jobs / self.jid / "meta.json").read_text())
        self.assertEqual(m["attempts_final"]["winner"], 2)              # try 2 was smallest
        self.assertEqual((repo / "one.py").read_text(), "1\n")          # now try 1's work is in the folder
        self.assertEqual((repo / "calc.py").read_text(), GOOD + "# try one\n")
        self.assertEqual(m.get("attempts_winner"), 1)
        self.assertIn(f"agent/{self.jid}-try1", m["attempts_note"])
        self.assertTrue(any("Using attempt 1 instead" in e["text"] for e in self.timeline()))

    def test_a_missing_branch_is_said_so(self):
        self.project()
        scen = {"attempts": [[{"write": "calc.py", "content": GOOD}],
                             [{"write": "calc.py", "content": GOOD + "# extra note\n"},
                              {"write": "NOTES.md", "content": "a\n" * 20}]]}
        m = self.run_job(scen, extra={"fix_rounds": 0, "attempts": 2, "hard": True},
                         then=("use-attempt", {"k": 4}))
        m = json.loads((self.jobs / self.jid / "meta.json").read_text())
        self.assertIn("That attempt is not in this job", m["attempts_note"])
        self.assertEqual(m["attempts_final"]["winner"], 1)              # the smaller try, as it was
        self.assertEqual((self.work / "proj" / "NOTES.md").exists(), False)

    def test_auto_attempts_queues_a_fresh_best_of_n_run(self):
        self.project()
        self.settings.write_text(json.dumps({"watchdog_tick_s": 1, "after_result_grace_s": 3,
                                             "auto_attempts": True, "attempts": 2}))
        m = self.run_job([{"say": "nope"}], extra={"fix_rounds": 0})
        self.assertEqual(m["verify"]["status"], "failed")
        self.assertEqual(self.child, [])                                # the button stays manual by default
        kids = [json.loads((self.jobs / d.name / "meta.json").read_text())
                for d in self.jobs.iterdir() if d.name != self.jid]
        self.assertEqual(len(kids), 1)
        self.assertEqual((kids[0]["attempts"], kids[0]["hard"]), (2, False))
        self.assertEqual(kids[0]["parent"], self.jid)
        self.assertEqual(kids[0]["job"], m["job"])
        self.assertIn(f"Starting {kids[0]['attempts']} fresh attempts (job {kids[0]['id']})",
                      [e["text"] for e in self.timeline()])

    def test_the_try_again_button_starts_one_without_the_setting(self):
        self.project()
        m = self.run_job([{"say": "nope"}], extra={"fix_rounds": 0}, retry=True)
        self.assertEqual(m["verify"]["status"], "failed")
        self.assertEqual(len(self.child), 1)
        kid = json.loads((self.jobs / self.child[0] / "meta.json").read_text())
        self.assertEqual((kid["attempts"], kid["hard"], kid["parent"]), (3, False, self.jid))

    def test_a_job_without_a_git_folder_just_runs_once(self):
        p = self.work / "docs"
        p.mkdir()
        (p / "README.md").write_text("hi\n")
        m = self.run_job([{"write": "README.md", "content": "hello\n"}], extra={"attempts": 3, "hard": True},
                         folder="docs")
        self.assertEqual(m["status"], "done")
        self.assertIn("needs a git folder", m["attempts_note"])
        self.assertNotIn("attempt_log", m)
        self.assertEqual((p / "README.md").read_text(), "hello\n")

    def test_each_try_starts_its_own_conversation(self):
        self.project()
        scen = {"attempts": [[{"write": "calc.py", "content": GOOD}], [{"write": "calc.py", "content": GOOD}]]}
        m = self.run_job(scen, extra={"fix_rounds": 0, "attempts": 2, "hard": True})
        sessions = [a["session"] for a in m["attempt_log"]]
        self.assertTrue(all(s and s.startswith("fake-") for s in sessions))
        self.assertNotEqual(sessions[0], sessions[1], "each try starts its own conversation")
        self.assertIn(m["session"], sessions)                     # follow-up carries on the kept try


# ------------------------------------------------------------------ the run, with a stand-in agent

class Run(unittest.TestCase):
    """run_best_of_n with a stand-in for run_agent_job, so the parts around the tries
    (preparing them, Stop, a branch that will not come home) can be seen on their own."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="u05-run-"))
        self.work = self.t / "work"
        self.repo = make_project(self.work)
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        self.jdir = self.t / "job"
        self.jdir.mkdir()
        self.jid = "20261111-120000-ab12"
        self.host = mock.patch.object(upgrades, "HOST_MODE", True)
        self.host.start()
        self.addCleanup(self.host.stop)
        self.rec = {"id": self.jid, "folder": "proj", "job": "fix calc", "status": "running",
                    "git_start": self.base, "attempts": 2, "hard": True}

    def drive(self, run_one):
        box = {"m": dict(self.rec)}

        def load(name):
            return box["m"]

        def update(name, **kw):
            box["m"].update(kw)
            return box["m"]

        out = attempts.run_best_of_n(box["m"], self.jid, work=self.work, job_dir=lambda n: self.jdir,
                                     load=load, update=update,
                                     run_one=lambda am, name, fresh: run_one(am, name, fresh, box["m"]),
                                     settings={})
        return out, box["m"]

    def branches(self, repo):
        return re.findall(r"agent/\S+", git(repo, "branch", "--list", "agent/*"))

    def test_a_try_that_cannot_start_fails_the_job_with_a_note(self):
        (self.work / attempts.attempt_folder("proj", self.jid, 1)).write_text("in the way\n")

        def no_agent(am, name, fresh, rec):
            raise AssertionError("the agent must not start when a try cannot be prepared")

        out, _ = self.drive(no_agent)
        self.assertEqual(out["status"], "failed")
        self.assertIn("Attempt 1 could not start", out["fail_note"])
        self.assertEqual(out["attempts_note"], out["fail_note"])
        self.assertEqual(out["attempts_final"]["ranked"], [])
        self.assertEqual(sorted(d.name for d in self.work.iterdir() if d.is_dir()), ["proj"])

    def test_stop_between_tries_ends_the_job_stopped_not_stopping(self):
        started = []

        def agent(am, name, fresh, rec):
            started.append(am["folder"])
            rec["status"] = "stopping"                     # the user pressed Stop during this try
            return {"status": "stopped", "session": "fake-1", "verify": {"status": "unverified"}}

        out, _ = self.drive(agent)
        self.assertEqual(out["status"], "stopped")
        self.assertEqual(out["fail_note"], "Stopped before all attempts finished.")
        self.assertEqual([a["k"] for a in out["attempt_log"]], [1])
        self.assertEqual(started, ["proj-try1-ab12"], "no further try starts after Stop")

    def test_a_try_that_came_back_stopped_does_not_start_the_next_one(self):
        """The real run_agent_job writes status="stopped" into the record itself, so by the
        time the loop looks there is no "stopping" left to find - Stop still wins."""
        started = []

        def agent(am, name, fresh, rec):
            started.append(am["folder"])
            rec.update(status="stopped", finished=1.0)     # exactly what run_agent_job leaves
            return dict(rec)

        out, _ = self.drive(agent)
        self.assertEqual(out["status"], "stopped")
        self.assertEqual(out["fail_note"], "Stopped before all attempts finished.")
        self.assertEqual([a["k"] for a in out["attempt_log"]], [1])
        self.assertEqual([a["status"] for a in out["attempt_log"]], ["stopped"])
        self.assertEqual(started, ["proj-try1-ab12"], "no further try starts after Stop")
        tl = [json.loads(l)["text"] for l in (self.jdir / "timeline.jsonl").read_text().splitlines()]
        self.assertIn("Stopped after attempt 1 of 2: no more tries", tl)

    def test_a_try_whose_branch_will_not_come_home_keeps_its_folder(self):
        def agent(am, name, fresh, rec):
            (self.work / am["folder"] / "work.py").write_text("print(1)\n")   # it did something
            return {"status": "done", "verify": {"status": "passed",
                                                 "final": {"results": [{"passed": 2, "failed": 0}]}}}

        def refusing(m, work, bundle, branch, sha=None):
            raise GitError("bundle went missing")

        with mock.patch.object(attempts, "bring_home", refusing):
            out, _ = self.drive(agent)
        self.assertEqual([a["k"] for a in out["attempt_log"]], [1, 2])
        for a in out["attempt_log"]:
            self.assertIn("branch could not be saved", a["note"])
            self.assertTrue((self.work / a["folder"]).is_dir(), "its copy is left where it is")
        self.assertEqual(out["attempts_final"]["ranked"], [1, 2])   # still scored, still ranked
        self.assertEqual(self.branches(self.repo), [])              # nothing came home to push
        self.assertIn("Could not put", out["attempts_note"])        # and the page says so


# ------------------------------------------------------------------ the pages

class Pages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="jb05-web-"))
        cls.saved = (jobboard.JOBS, jobboard.WORK, jobboard.PORT, jobboard.READONLY)
        jobboard.JOBS = cls.tmp / "jobs"
        jobboard.JOBS.mkdir()
        jobboard.WORK = cls.tmp / "work"
        jobboard.READONLY = False
        repo = make_project(jobboard.WORK)
        cls.base = git(repo, "rev-parse", "HEAD").strip()
        jid = "20261111-120000-ab12"
        for k, content in ((1, GOOD + "# one\n"), (2, GOOD)):
            git(repo, "update-ref", f"refs/heads/agent/{jid}-try{k}", cls.base)
            wt = cls.tmp / f"seed{k}"
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "-q", "--no-checkout",
                            str(repo), str(wt)], check=True)
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t", "-c", "user.email=t@t",
                            "checkout", "-q", "-B", f"agent/{jid}-try{k}"], cwd=str(wt), check=True)
            (wt / "calc.py").write_text(content)
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t", "-c", "user.email=t@t",
                            "add", "-A"], cwd=str(wt), check=True)
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t", "-c", "user.email=t@t",
                            "commit", "-qm", f"try {k}"], cwd=str(wt), check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(wt), check=True,
                                 capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=always",
                            "fetch", "-q", str(wt), f"agent/{jid}-try{k}:refs/heads/agent/{jid}-try{k}"],
                           cwd=str(repo), check=True)
            setattr(cls, f"sha{k}", sha)
        m = {"id": jid, "mode": "local-qwen", "folder": "proj", "job": "fix calc", "offline": True,
             "status": "done", "created": time.time() - 900, "started": time.time() - 800,
             "finished": time.time() - 100, "exit": 0, "resume": None, "parent": None, "session": "s" * 12,
             "git_start": cls.base, "git_branch_start": "main", "attempts": 2, "hard": True,
             "attempts_total": 2, "attempts_final": {"winner": 2, "why": "Attempt 2 won: a smaller change."},
             "attempt_log": [{"k": 1, "branch": f"agent/{jid}-try1", "status": "passed", "commit": cls.sha1,
                              "tests_passed": 2, "test_files_changed": 0, "diff_lines": 12,
                              "files": [["calc.py", 7, 2], ["one.py", 3, 0]], "seconds": 61.0, "issues": []},
                             {"k": 2, "branch": f"agent/{jid}-try2", "status": "passed", "commit": cls.sha2,
                              "tests_passed": 2, "test_files_changed": 0, "diff_lines": 1,
                              "files": [["calc.py", 1, 1]], "seconds": 40.0, "issues": []}],
             "verify": {"status": "passed", "banner": {"colour": "green", "text": "Checks passed (2 tests)"}}}
        (jobboard.JOBS / jid).mkdir(parents=True, exist_ok=True)   # save_meta writes into this dir
        jobboard.save_meta(m, jid)
        cls.jid, cls.repo = jid, repo
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()
        jobboard.PORT = cls.port                       # guard() reads this at request time
        cls.host = mock.patch.object(upgrades, "HOST_MODE", True)
        cls.host.start()
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), jobboard.H)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thr.join(timeout=5)
        cls.host.stop()
        jobboard.JOBS, jobboard.WORK, jobboard.PORT, jobboard.READONLY = cls.saved

    def fetch(self, path, data=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body)
        req.add_header("Cookie", f"jb={jobboard.TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.geturl()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), ""

    def meta(self):
        return json.loads((jobboard.JOBS / self.jid / "meta.json").read_text())

    def test_the_new_job_form_offers_the_hard_option(self):
        code, html, _ = self.fetch("/")
        self.assertEqual(code, 200)
        self.assertIn('name="hard"', html)
        self.assertIn("Hard job: try it 3 times", html)
        self.assertIn('name="attempts"', html)
        self.assertIn('min="2" max="4"', html)

    def test_the_job_page_shows_every_attempt(self):
        code, html, _ = self.fetch(f"/job/{self.jid}")
        self.assertEqual(code, 200)
        self.assertIn("<h2>Attempts</h2>", html)
        self.assertIn("Attempt 2 won: a smaller change.", html)
        self.assertIn(f"agent/{self.jid}-try1", html)
        self.assertIn("2 files, 12 lines", html)
        self.assertIn("1.0 min", html)
        self.assertIn(f'action="/use-attempt/{self.jid}"', html)
        self.assertIn("for 7 days", html)

    def test_use_this_one_instead_moves_the_job_branch(self):
        code, _, url = self.fetch(f"/use-attempt/{self.jid}", {"k": "1"})
        self.assertEqual(code, 200)
        self.assertTrue(url.endswith(f"/job/{self.jid}"))
        m = self.meta()
        self.assertEqual(m["attempts_winner"], 1)
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip(), f"agent/{self.jid}")
        self.assertEqual(git(self.repo, "rev-parse", f"agent/{self.jid}").strip(), self.sha1)
        self.assertEqual((self.repo / "calc.py").read_text(), GOOD + "# one\n")
        # and the other way back
        self.fetch(f"/use-attempt/{self.jid}", {"k": "2"})
        self.assertEqual((self.repo / "calc.py").read_text(), GOOD)

    def test_use_this_one_instead_refuses_a_try_that_is_not_there(self):
        code, _, _ = self.fetch(f"/use-attempt/{self.jid}", {"k": "9"})
        self.assertEqual(code, 200)
        self.assertIn("That attempt is not in this job", self.meta()["attempts_note"])

    def test_try_hard_starts_a_fresh_best_of_n_job(self):
        jid2 = "20261111-120001-cd34"
        m = dict(self.meta(), id=jid2, attempts=None, attempts_final=None, attempt_log=None,
                 status="failed", verify={"status": "failed",
                                          "banner": {"colour": "red", "text": "Failed after 3 fix rounds"}})
        m.pop("attempts", None)
        (jobboard.JOBS / jid2).mkdir(parents=True, exist_ok=True)
        jobboard.save_meta(m, jid2)
        code, html, url = self.fetch(f"/job/{jid2}")
        self.assertIn('action="/try-hard/', html)
        code, _, url = self.fetch(f"/try-hard/{jid2}", {})
        self.assertEqual(code, 200)
        kid = re.search(r"/job/(\d{8}-\d{6}-[0-9a-f]{4})$", url).group(1)
        k = json.loads((jobboard.JOBS / kid / "meta.json").read_text())
        self.assertEqual((k["attempts"], k["hard"], k["parent"]), (3, False, jid2))
        self.assertEqual(k["status"], "queued")
        # a job that already ran its tries never gets a second round of buttons
        self.assertNotIn('action="/try-hard/', self.fetch(f"/job/{self.jid}")[1])

    def test_a_bad_attempt_count_is_refused_on_the_form(self):
        code, html, _ = self.fetch("/new", {"mode": "local-qwen", "folder": "proj", "job": "do it",
                                            "hard": "on", "attempts": "9"})
        self.assertEqual(code, 400)
        self.assertIn("2 to 4 times", html)
        code, _, url = self.fetch("/new", {"mode": "local-qwen", "folder": "proj", "job": "do it",
                                           "hard": "on", "attempts": "2"})
        self.assertEqual(code, 200)
        kid = re.search(r"/job/(\d{8}-\d{6}-[0-9a-f]{4})$", url).group(1)
        k = json.loads((jobboard.JOBS / kid / "meta.json").read_text())
        self.assertEqual((k["attempts"], k["hard"]), (2, True))


if __name__ == "__main__":
    unittest.main()
