"""Upgrade 8: approve & merge / ask for changes / reject (web/decisions.py + the job page).

The merge engine itself (agentjobs_upgrades/actions.py) is tested in test_rest.py. These
tests are about the glue: which branch gets merged into which, what the record and the
timeline say, what happens on a conflict, and that the buttons only work for a person
sitting at the page - never for a job running on the same machine.

git runs straight on the temp folders (JOBBOARD_JAIL=host), like the other suites.
Nothing here touches a device, the model server or a real agent.
"""
import http.server
import json
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
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

import decisions
import dashboard
import jobboard
import upgrades
from agentjobs_upgrades import watchdog

HERE = Path(__file__).resolve().parent
WEB = HERE.parent
JAIL = WEB.parent / "jail"


def git(repo, *a):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t",
                           "-c", "user.email=t@t", *a], cwd=str(repo), check=True,
                          capture_output=True, text=True).stdout


def timeline(job_dir):
    p = Path(job_dir) / "timeline.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def make_repo(root, jid="j1", branch="agent/j1", base_branch="main", on_job_branch=True):
    """main with one commit, the job's work on agent/j1, the folder left where an agent
    would leave it. Returns the job's starting commit."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", base_branch)
    (root / "a.txt").write_text("one\n")
    (root / "PLAN.md").write_text("# Plan\n- [x] did the thing\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD").strip()
    git(root, "checkout", "-qb", branch)
    (root / "feature.py").write_text("def added():\n    return 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", f"job {jid}: added the thing")
    if not on_job_branch:
        git(root, "checkout", "-q", base_branch)
    return base


def meta_of(jid="j1", folder="proj", base="", branch_start="main", verify=("passed",), **kw):
    m = {"id": jid, "mode": "local-qwen", "folder": folder, "job": "Add the thing to the app",
         "offline": True, "status": "done", "created": 1000.0, "started": 1001.0,
         "finished": 1100.0, "exit": 0, "resume": None, "parent": None, "session": None,
         "git_start": base, "git_branch_start": branch_start}
    if verify:
        m["verify"] = {"status": verify[0]}
    m.update(kw)
    return m


class Fake:
    """The four board calls decisions.py makes, on temp folders: no HTTP, no worker."""

    def __init__(self, root, meta):
        self.tmp = root.parent.parent
        self.work = root.parent
        self.jobs = self.tmp / "jobs"
        self.metas = {meta["id"]: meta}
        self.created = []
        self._n = 0

    def ctx(self):
        return dict(work=self.work, load=lambda jid: self.metas.get(jid), update=self.update,
                    job_dir=lambda jid: self.jobs / jid, new_job=self.new_job)

    def update(self, jid, **kw):
        m = self.metas.get(jid)
        if m is None:
            return None
        m.update(kw)
        return m

    def new_job(self, mode, folder, job, offline, resume=None, parent=None, extra=None):
        self._n += 1
        m = {"id": "20261112-12000%d-%04x" % (self._n, self._n), "mode": mode, "folder": folder,
             "job": job, "offline": offline, "status": "queued", "resume": resume,
             "parent": parent, "extra": extra or {}}
        self.created.append(m)
        self.metas[m["id"]] = m
        return m

    def log(self, jid="j1"):
        return self.metas[jid].get("decision_log") or []

    def last(self, kind, jid="j1"):
        return next((e for e in reversed(self.log(jid)) if e.get("kind") == kind), None)


class Base(unittest.TestCase):
    """A repo + a finished job record + the fake board, ready to decide."""

    jid = "j1"
    verify = ("passed",)
    branch = "agent/j1"
    base_branch = "main"

    def setUp(self):
        decisions._hint_cache.clear()
        self.tmp = Path(tempfile.mkdtemp(prefix="u08-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.secret = self.tmp / "state" / "human-secret"
        self.p_secret = mock.patch.object(decisions, "SECRET_FILE", self.secret)
        self.p_secret.start()
        self.p_host = mock.patch.object(upgrades, "HOST_MODE", True)
        self.p_host.start()
        self.addCleanup(self.p_host.stop)
        self.root = self.tmp / "work" / "proj"
        self.base = make_repo(self.root, self.jid, self.branch, self.base_branch)
        self.meta = meta_of(self.jid, base=self.base, branch_start=self.base_branch,
                            verify=self.verify)
        self.f = Fake(self.root, self.meta)
        self.ctx = self.f.ctx()

    def get_form(self, **kw):
        form = {k: [v] for k, v in kw.items()}
        return lambda k: (form.get(k, [""])[0]).strip()

    def act(self, act, **fields):
        decisions.action(self.jid, act, self.get_form(**fields), self.ctx)

    def tl(self):
        return timeline(self.f.jobs / self.jid)

    def kinds(self):
        return [e["type"] for e in self.tl()]

    def head(self, branch=None):
        return git(self.root, "rev-parse", branch or "HEAD").strip()

    def tree_of(self, branch):
        return git(self.root, "rev-parse", f"{branch}^{{tree}}").strip()

    def dirty(self):
        return git(self.root, "status", "--porcelain").strip()


# ------------------------------------------------------------------ which branch, into which

class Branches(Base):
    def test_the_job_branch_is_the_one_the_folder_is_still_on(self):
        target = decisions.target_branch(self.meta, upgrades.git_for(self.meta, self.root))
        self.assertEqual(target, "main")                     # from git_branch_start
        branch = decisions.job_branch(self.meta, upgrades.git_for(self.meta, self.root), target)
        self.assertEqual(branch, "agent/j1")

    def test_the_target_is_the_branch_the_job_started_from(self):
        git(self.root, "checkout", "-qb", "develop")
        self.meta["git_branch_start"] = "develop"
        git(self.root, "checkout", "-q", "main")
        self.assertEqual(decisions.target_branch(self.meta, upgrades.git_for(self.meta, self.root)),
                         "develop")

    def test_a_record_without_a_starting_branch_falls_back_to_main(self):
        self.meta.pop("git_branch_start")
        self.assertEqual(decisions.target_branch(self.meta, upgrades.git_for(self.meta, self.root)),
                         "main")

    def test_a_project_whose_only_branch_is_master_uses_master(self):
        root = self.tmp / "work" / "old"
        base = make_repo(root, self.jid, "agent/j1", base_branch="master")
        m = meta_of(self.jid, folder="old", base=base, branch_start="")
        self.assertEqual(decisions.target_branch(m, upgrades.git_for(m, root)), "master")

    def test_the_best_of_n_winner_branch_wins_over_the_current_one(self):
        g = upgrades.git_for(self.meta, self.root)
        git(self.root, "checkout", "-qb", "agent/leftover")
        (self.root / "x.py").write_text("x = 1\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "a leftover branch from the same job")
        self.meta["attempts"] = 2
        self.assertEqual(decisions.job_branch(self.meta, g, "main"), "agent/j1")

    def test_try_branches_of_a_best_of_n_job_are_never_merged_whole(self):
        g = upgrades.git_for(self.meta, self.root)
        git(self.root, "branch", "agent/j1-try2")                    # a losing attempt
        self.assertNotEqual(decisions.job_branch(self.meta, g, "main"), "agent/j1-try2")

    def test_a_branch_already_merged_into_the_target_is_not_offered(self):
        g = upgrades.git_for(self.meta, self.root)
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--ff-only", "agent/j1")
        self.assertEqual(decisions.job_branch(self.meta, g, "main"), "")

    def test_the_branch_carrying_this_job_s_checkpoints_wins_over_a_newer_one(self):
        g = upgrades.git_for(self.meta, self.root)
        tip = git(self.root, "rev-parse", "agent/j1").strip()
        self.meta["checkpoints"] = {"checkpoints": [{"n": 1, "commit": tip, "step": "did it"}]}
        git(self.root, "checkout", "-q", "main")
        git(self.root, "checkout", "-qb", "agent/other-job")
        (self.root / "z.py").write_text("z = 1\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "a newer job in the same folder")
        git(self.root, "checkout", "-q", "main")
        self.assertEqual(decisions.job_branch(self.meta, g, "main"), "agent/j1")

    def test_the_card_names_the_branch_it_means_to_merge(self):
        git(self.root, "checkout", "-q", "main")
        git(self.root, "checkout", "-qb", "agent/other-job")
        (self.root / "z.py").write_text("z = 1\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "a newer job in the same folder")
        git(self.root, "checkout", "-q", "main")
        html = decisions.card_html(self.meta, self.jid, True, "t", self.root)
        want = decisions.job_branch(self.meta, upgrades.git_for(self.meta, self.root), "main")
        self.assertIn("agent/", want)                       # the board guesses, and says which
        self.assertIn(f"Merges <code>{want}</code> into <code>main</code>", html)

    def test_a_branch_name_that_is_not_a_branch_name_is_ignored(self):
        g = upgrades.git_for(self.meta, self.root)
        out = decisions.heads(g)
        self.assertIn("agent/j1", [n for n, _ in out])
        self.meta["git_branch_start"] = "main; rm -rf /"
        self.assertEqual(decisions.target_branch(self.meta, g, out), "main")


# ------------------------------------------------------------------ approve & merge

class Merge(Base):
    def test_fast_forward_when_main_did_not_move(self):
        self.act("merge")
        self.assertEqual(self.head("main"), self.head("agent/j1"))
        self.assertTrue((self.root / "feature.py").exists())
        e = self.f.last("merge")
        self.assertEqual((e["target"], e["branch"], e["how"], e["pushed"]),
                         ("main", "agent/j1", "fast-forward", False))
        self.assertEqual(self.meta["decision"], "merged")
        self.assertIn("merged", self.kinds())

    def test_a_merge_commit_when_main_moved(self):
        git(self.root, "checkout", "-q", "main")
        (self.root / "a.txt").write_text("one\ntwo\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "main moved on")
        git(self.root, "checkout", "-q", "agent/j1")
        self.act("merge")
        e = self.f.last("merge")
        self.assertEqual(e["how"], "merge commit")
        self.assertIn("Merge agent job j1: Add the thing to the app",
                      git(self.root, "log", "-1", "--format=%s", "main"))

    def test_the_record_keeps_the_commit_to_go_back_to(self):
        git(self.root, "checkout", "-q", "main")
        (self.root / "a.txt").write_text("one\ntwo\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "main moved on")
        before = git(self.root, "rev-parse", "main").strip()
        git(self.root, "checkout", "-q", "agent/j1")
        self.act("merge")
        e = self.f.last("merge")
        self.assertEqual((e["before"], e["after"]), (before, self.head("main")))

    def test_the_job_branch_is_left_checked_out_afterwards(self):
        self.act("merge")
        self.assertEqual(git(self.root, "rev-parse", "--abbrev-ref", "HEAD").strip(), "agent/j1")

    def test_nothing_is_pushed_and_the_record_says_so(self):
        self.act("merge")
        self.assertFalse(self.f.last("merge")["pushed"])
        self.assertNotIn("push", self.meta)         # the board never pushes main

    def test_the_timeline_gets_the_decision(self):
        self.act("merge")
        rows = {e["type"]: e["text"] for e in self.tl()}
        self.assertIn("Merged agent/j1 into main (fast-forward) - not pushed", rows["merged"])

    def test_a_folder_with_work_in_progress_is_left_alone(self):
        (self.root / "uncommitted.py").write_text("wip = 1\n")
        self.act("merge")
        self.assertIsNone(self.f.last("merge"))
        self.assertIn("uncommitted", self.f.last("refused")["reason"])
        self.assertEqual(self.head("main"), self.base)

    def test_a_folder_that_is_not_a_repository_is_said_so(self):
        self.meta["folder"] = "nowhere"
        self.act("merge")
        self.assertIn("not a git repository", self.f.last("refused")["reason"])

    def test_nothing_to_merge_says_so_instead_of_lieing(self):
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--ff-only", "agent/j1")
        self.act("merge")
        self.assertIn("no branch with changes of its own", self.f.last("refused")["reason"])


# ------------------------------------------------------------------ the checks gate

class Gate(Base):
    def check(self, status, typed="", merged=None):
        self.meta.pop("verify", None)
        self.meta.pop("verify_off", None)
        if status == "off":
            self.meta["verify_off"] = True
        elif status:
            self.meta["verify"] = {"status": status}
        self.act("merge", typed=typed)
        self.assertEqual(bool(self.f.last("merge")), merged, status)

    def test_checks_passed_merges(self):
        self.check("passed", merged=True)

    def test_failed_checks_do_not_merge(self):
        self.check("failed", merged=False)
        self.assertIn("Checks didn't pass", self.f.last("refused")["reason"])

    def test_checks_that_were_never_run_do_not_merge(self):
        self.check(None, merged=False)
        self.assertEqual(decisions.gate(self.meta)[1], "The checks were not run.")

    def test_a_job_the_gate_did_not_check_is_not_mergeable(self):
        self.check("off", merged=False)

    def test_needs_review_and_stuck_are_not_mergeable(self):
        for st in ("needs_review", "stuck", "aborted", "unverified"):
            self.check(st, merged=False)

    def test_typing_MERGE_merges_anyway_and_says_which_it_was(self):
        self.check("failed", typed="MERGE", merged=True)
        self.assertEqual(self.f.last("merge")["how"], "fast-forward")
        self.assertEqual(self.head("main"), self.head("agent/j1"))

    def test_the_word_has_to_be_MERGE(self):
        for word in ("merge", "Merge", "merge anyway", ""):
            self.check("failed", typed=word, merged=False)

    def test_the_card_greys_the_button_and_offers_merge_anywhere(self):
        self.meta["verify"] = {"status": "failed"}
        html = decisions.card_html(self.meta, self.jid, True, "tok", self.root)
        self.assertIn("<button disabled", html)
        self.assertIn("Merge into main", html)
        self.assertIn("Merge anyway", html)
        self.assertIn('name="typed"', html)
        self.assertIn("The checks failed.", html)

    def test_the_card_offers_a_plain_button_when_checks_passed(self):
        html = decisions.card_html(self.meta, self.jid, True, "tok", self.root)
        self.assertIn("<button>Merge into main</button>", html)
        self.assertNotIn("Merge anyway", html)


# ------------------------------------------------------------------ conflicts

class Conflict(Base):
    def setUp(self):
        super().setUp()
        git(self.root, "checkout", "-q", "agent/j1")
        (self.root / "a.txt").write_text("the job's line\n")
        git(self.root, "commit", "-qam", "job changed a.txt")
        git(self.root, "checkout", "-q", "main")
        (self.root / "a.txt").write_text("main's own line\n")
        git(self.root, "commit", "-qam", "main changed a.txt")
        git(self.root, "checkout", "-q", "agent/j1")

    def test_the_merge_is_aborted_and_everything_is_left_as_it_was(self):
        main_head, main_tree = self.head("main"), self.tree_of("main")
        job_head, job_tree = self.head("agent/j1"), self.tree_of("agent/j1")
        self.act("merge")
        self.assertEqual((self.head("main"), self.tree_of("main")), (main_head, main_tree))
        self.assertEqual((self.head("agent/j1"), self.tree_of("agent/j1")), (job_head, job_tree))
        self.assertEqual(self.dirty(), "")
        self.assertEqual(git(self.root, "rev-parse", "--abbrev-ref", "HEAD").strip(), "agent/j1")
        self.assertIsNone(self.f.last("merge"))

    def test_the_conflict_goes_into_the_record_and_the_timeline(self):
        self.act("merge")
        e = self.f.last("conflict")
        self.assertEqual((e["target"], e["branch"]), ("main", "agent/j1"))
        self.assertIn("clash", e["reason"])
        self.assertIn("needs_review", self.kinds())
        self.assertEqual(self.meta["decision"], "conflict")

    def test_the_card_offers_to_ask_qwen_to_fix_it(self):
        self.act("merge")
        html = decisions.card_html(self.meta, self.jid, True, "tok", self.root)
        self.assertIn("Ask Qwen to fix the conflict", html)
        self.assertIn('value="fix-conflict"', html)

    def test_asking_starts_a_follow_up_job_that_fixes_it_on_the_job_branch(self):
        self.act("merge")
        self.act("fix-conflict")
        child = self.f.created[0]
        self.assertEqual((child["parent"], child["folder"], child["mode"]),
                         (self.jid, "proj", "local-qwen"))
        for want in ("merge main into it", "agent/j1", "Resolve every conflict",
                     "Do not merge into main yourself", "never push"):
            self.assertIn(want, child["job"])
        self.assertIn("Add the thing to the app", child["job"])       # the original job is quoted
        self.assertEqual(self.f.last("fix-conflict")["job"], child["id"])

    def test_the_fix_job_is_offered_once(self):
        self.act("merge")
        self.act("fix-conflict")
        html = decisions.card_html(self.meta, self.jid, True, "tok", self.root)
        self.assertNotIn("Ask Qwen to fix the conflict", html)
        self.assertIn("A job is sorting the clash out", html)


# ------------------------------------------------------------------ ask for changes

class AskForChanges(Base):
    def test_the_text_box_starts_a_follow_up_job_on_the_same_branch(self):
        self.meta["session"] = "sess-12345678"
        self.meta["checks"] = [{"name": "tests", "cmd": "make test", "timeout_s": 60}]
        self.meta["fix_rounds"] = 3
        self.act("ask", request="Make the button blue, not green.")
        child = self.f.created[0]
        self.assertEqual((child["parent"], child["folder"], child["resume"]),
                         (self.jid, "proj", "sess-12345678"))
        self.assertIn("Make the button blue, not green.", child["job"])
        self.assertIn("Add the thing to the app", child["job"])       # the original prompt
        self.assertIn("Jason asked for these changes", child["job"])
        self.assertEqual(child["extra"]["checks"], self.meta["checks"])
        self.assertEqual(child["extra"]["fix_rounds"], 3)
        self.assertEqual(self.meta["decision"], "changes")
        self.assertIn("changes", self.kinds())

    def test_a_job_with_no_saved_session_still_gets_its_follow_up(self):
        self.act("ask", request="Rename the file.")
        self.assertIsNone(self.f.created[0]["resume"])

    def test_an_empty_box_changes_nothing(self):
        self.act("ask", request="   ")
        self.assertEqual(self.f.created, [])
        self.assertEqual(self.f.last("refused")["reason"], "Write what you want changed first.")


# ------------------------------------------------------------------ reject and undo

class Reject(Base):
    def test_reject_renames_the_branch_and_keeps_the_folder(self):
        self.act("reject")
        branches = git(self.root, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
        self.assertIn("agent/rejected/j1", branches.split())
        self.assertNotIn("agent/j1", branches.split())
        self.assertTrue((self.root / "feature.py").exists())
        e = self.f.last("reject")
        self.assertEqual((e["branch"], e["old_branch"]), ("agent/rejected/j1", "agent/j1"))
        self.assertEqual(self.meta["decision"], "rejected")
        self.assertIn("rejected", self.kinds())

    def test_undo_puts_the_branch_back(self):
        self.act("reject")
        self.act("undo-reject")
        branches = git(self.root, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
        self.assertIn("agent/j1", branches.split())
        self.assertNotIn("agent/rejected/j1", branches.split())
        self.assertIn("restart", self.kinds())

    def test_undo_is_only_possible_for_14_days(self):
        self.act("reject")
        self.f.last("reject")["time"] -= 15 * 86400
        self.act("undo-reject")
        self.assertIn("Older than 14 days", self.f.last("refused")["reason"])
        self.assertIn("agent/rejected/j1", git(self.root, "branch"))

    def test_the_card_shows_the_rejection_and_the_undo(self):
        self.act("reject")
        html = decisions.card_html(self.meta, self.jid, True, "tok", self.root)
        self.assertIn("Rejected on", html)
        self.assertIn("agent/rejected/j1", html)
        self.assertIn('value="undo-reject"', html)

    def test_a_rejected_job_offers_no_merge(self):
        self.act("reject")
        html = decisions.card_html(self.meta, self.jid, True, "tok", self.root)
        self.assertNotIn("Merge into", html)
        self.assertNotIn('value="merge"', html)

    def test_a_rejected_job_offers_undo_only(self):
        self.act("reject")
        html = decisions.card_html(self.meta, self.jid, True, "tok", self.root)
        for gone in ('value="reject"', 'value="ask"', "Ask for changes"):
            self.assertNotIn(gone, html)
        self.assertIn('value="undo-reject"', html)
        self.act("undo-reject")                       # and Undo brings the decisions back
        html = decisions.card_html(self.meta, self.jid, True, "tok", self.root)
        self.assertIn('<button class="ghost">Reject</button>', html)
        self.assertIn('value="ask"', html)


class UndoMerge(Base):
    def test_undo_puts_the_target_back_where_it_was(self):
        git(self.root, "checkout", "-q", "main")
        (self.root / "a.txt").write_text("one\ntwo\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "main moved on")
        before = git(self.root, "rev-parse", "main").strip()
        git(self.root, "checkout", "-q", "agent/j1")
        self.act("merge")
        self.act("undo-merge")
        self.assertEqual(self.head("main"), before)
        self.assertNotIn("feature.py", git(self.root, "ls-tree", "--name-only", "main").split())
        self.assertIn("feature.py", git(self.root, "ls-tree", "--name-only", "agent/j1").split())
        self.assertEqual(self.meta["decision"], "merge-undone")
        self.assertIn("rollback", self.kinds())

    def test_undo_refuses_when_somebody_committed_on_top_since(self):
        self.act("merge")
        git(self.root, "checkout", "-q", "main")
        (self.root / "later.py").write_text("later = 1\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "work on top of the merge")
        moved = self.head("main")
        git(self.root, "checkout", "-q", "agent/j1")
        self.act("undo-merge")
        self.assertIn("Something was committed on top", self.f.last("refused")["reason"])
        self.assertEqual(self.head("main"), moved)

    def test_the_undo_button_disappears_once_it_is_used(self):
        self.act("merge")
        self.assertIn('value="undo-merge"', decisions.card_html(self.meta, self.jid, True, "t", self.root))
        self.act("undo-merge")
        self.assertNotIn('value="undo-merge"', decisions.card_html(self.meta, self.jid, True, "t", self.root))

    def test_undo_is_only_offered_for_the_last_merge(self):
        self.act("merge")
        self.act("undo-merge")
        self.act("undo-merge")                       # nothing to undo any more
        self.assertIsNone(self.f.last("refused"))


# ------------------------------------------------------------------ the human-only secret

class SecretOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u08sec-"))
        self.p = mock.patch.object(decisions, "SECRET_FILE", self.tmp / "state" / "secret")
        self.p.start()
        self.addCleanup(self.p.stop)
        self.sid = decisions.new_sid()

    def test_the_secret_file_is_written_once_and_only_readable_by_its_owner(self):
        s = decisions.secret()
        self.assertEqual(oct(self.secret_path().stat().st_mode & 0o777), "0o600")
        self.assertEqual(decisions.secret(), s)

    def secret_path(self):
        return self.tmp / "state" / "secret"

    def test_a_post_with_the_cookie_and_its_own_token_is_allowed(self):
        tok = decisions.token_for(self.sid)
        self.assertTrue(decisions.allowed("POST", f"aj={self.sid}", tok))

    def test_a_get_is_never_allowed(self):
        self.assertFalse(decisions.allowed("GET", f"aj={self.sid}", decisions.token_for(self.sid)))

    def test_no_cookie_no_token(self):
        self.assertFalse(decisions.allowed("POST", "", decisions.token_for(self.sid)))
        self.assertFalse(decisions.allowed("POST", f"aj={self.sid}", ""))
        self.assertFalse(decisions.allowed("POST", f"aj={self.sid}", None))

    def test_a_token_stolen_from_another_session_is_useless(self):
        other = decisions.new_sid()
        self.assertFalse(decisions.allowed("POST", f"aj={other}", decisions.token_for(self.sid)))

    def test_a_forged_or_guessed_token_is_refused(self):
        for guess in ("", "0" * 64, decisions.token_for("x" * 24), "MERGE"):
            self.assertFalse(decisions.allowed("POST", f"aj={self.sid}", guess), guess)

    def test_a_cookie_that_is_not_a_session_id_is_refused(self):
        for bad in ("short", "../x", "a" * 200, "aj=x"):
            self.assertFalse(decisions.allowed("POST", f"aj={bad}", decisions.token_for(bad)), bad)

    def test_the_page_never_sees_the_secret_itself(self):
        s = decisions.secret()
        tok = decisions.token_for(self.sid)
        self.assertNotEqual(tok, s)
        self.assertNotIn(s, tok)
        self.assertEqual(len(tok), 64)                     # an HMAC, not the secret

    def test_the_session_cookie_is_HttpOnly_and_same_site(self):
        h = decisions.cookie_header(self.sid)
        for want in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(want, h)


class JailCannotReadIt(unittest.TestCase):
    """Requirement 6: the secret is not reachable from inside the jail the jobs run in."""

    def test_the_default_secret_is_not_in_the_home_the_container_gets(self):
        secret = str(decisions.actions.DEFAULT_SECRET)
        self.assertNotIn("agent-work", secret)
        for mounted in ("state/home-local", "state/android-sdk", "agent-work"):
            self.assertFalse(secret.startswith(str(Path.home() / "agent-sandbox" / mounted)), secret)

    def test_the_jail_scripts_mount_nothing_that_contains_it(self):
        args = (JAIL / "podman-local.sh").read_text(encoding="utf-8")
        agent = (JAIL / "agent-jail.sh").read_text(encoding="utf-8")
        mounts = re.findall(r'-v "([^"]+):', args)
        self.assertTrue(mounts)
        secret = Path(decisions.actions.DEFAULT_SECRET).resolve()
        for src in mounts:
            src = src.replace("$HOME", str(Path.home())).replace("$JAIL_STATE",
                                                                 str(Path.home() / "agent-sandbox/state"))
            src = src.replace("$JAIL_HOME_LOCAL", str(Path.home() / "agent-sandbox/state/home-local"))
            src = src.replace("$JAIL_DIR", str(Path.home() / "agent-sandbox/jail"))
            src = src.replace("$hostdir", "PROJECT").replace("$JAIL_RUNDIR", "/run/basalt")
            if "PROJECT" in src or "mktemp" in src or "{" in src:
                continue
            self.assertNotIn(str(secret.parent), Path(src).resolve().as_posix(), src)
            self.assertFalse(str(secret).startswith(Path(src).resolve().as_posix()), src)
        self.assertIn('"$HOME/.local"', agent)      # and the bwrap jail refuses it as a project

    def test_a_job_that_goes_looking_for_it_is_stopped(self):
        for cmd in ("cat ~/.local/state/agentjobs/secret", "ls $HOME/.local/state/agentjobs",
                    "cat /home/jason/.local/state/agentjobs/secret"):
            self.assertIn("private location", watchdog.safety_reason(cmd) or "", cmd)

    def test_a_normal_project_command_is_still_fine(self):
        self.assertIsNone(watchdog.safety_reason("cat .local/state/notes.md"))
        self.assertIsNone(watchdog.safety_reason("python3 -m unittest discover -s tests"))


# ------------------------------------------------------------------ what a press is checked against

class Guard(unittest.TestCase):
    def test_a_finished_job_in_a_real_folder_can_be_decided(self):
        self.assertEqual(decisions.cannot_decide(meta_of()), "")

    def test_a_job_that_has_not_finished_cannot(self):
        for st in ("queued", "running", "stopping"):
            self.assertIn("has to finish", decisions.cannot_decide(meta_of(status=st)), st)

    def test_a_bench_run_is_not_a_job(self):
        self.assertIn("bench run", decisions.cannot_decide(meta_of(kind="bench")))

    def test_a_folder_name_that_is_not_a_folder_never_reaches_git(self):
        for bad in ("../elsewhere", "a/b", "eval", "", None, 7):
            self.assertIn("folder", decisions.cannot_decide(meta_of(folder=bad)), repr(bad))

    def test_a_folder_another_job_is_still_working_in_waits(self):
        self.assertIn("still working", decisions.cannot_decide(meta_of(), busy=True))

    def test_the_worker_s_launch_check_is_not_the_test_for_a_decision(self):
        """jobboard.check_record refuses a record whose parent job is gone. That says nothing
        about approving the work the job left behind, and used to swallow the press silently."""
        self.assertEqual(decisions.cannot_decide(meta_of(parent="20260101-000000-ffff")), "")


# ------------------------------------------------------------------ the card

class Card(Base):
    def test_a_running_job_or_a_bench_run_gets_no_card(self):
        self.meta["status"] = "running"
        self.assertEqual(decisions.card_html(self.meta, self.jid, True, "t", self.root), "")
        self.meta["status"] = "done"
        self.meta["kind"] = "bench"
        self.assertEqual(decisions.card_html(self.meta, self.jid, True, "t", self.root), "")

    def test_the_three_decisions_are_there_with_the_hidden_token(self):
        html = decisions.card_html(self.meta, self.jid, True, "tok", self.root)
        self.assertIn('<h2>Approve or reject</h2>', html)
        self.assertIn("<button>Merge into main</button>", html)
        self.assertIn("Ask for changes", html)
        self.assertIn('<button class="ghost">Reject</button>', html)
        self.assertIn('name="ajtok" value="tok"', html)
        self.assertEqual(html.count('action="/decide/j1"'), 3)

    def test_nothing_is_offered_when_the_board_may_not_act(self):
        html = decisions.card_html(self.meta, self.jid, False, "", self.root)
        for no in ("Ask for changes", "Reject", "Merge into main"):
            self.assertNotIn(no, html)
        self.assertIn("All checks passed.", html)

    def test_a_copy_that_cannot_ask_git_still_names_the_record_s_branch(self):
        self.meta["git_branch_start"] = "develop"
        self.assertEqual(decisions.target_word(self.meta), "develop")
        self.meta.pop("git_branch_start")
        self.assertEqual(decisions.target_word(self.meta), "main")
        self.meta["git_branch_start"] = "not a branch name"
        self.assertEqual(decisions.target_word(self.meta), "main")

    def test_the_merged_state_says_not_pushed(self):
        self.act("merge")
        html = decisions.card_html(self.meta, self.jid, True, "t", self.root)
        self.assertIn("Merged <code>agent/j1</code> into <code>main</code>", html)
        self.assertIn("Not pushed", html)
        self.assertIn("only ever pushes agent/... branches", html)

    def test_a_refusal_shows_once_and_then_gone(self):
        self.meta["verify"] = {"status": "failed"}
        self.act("merge")
        self.assertIn("Type MERGE to merge anyway",
                      decisions.card_html(self.meta, self.jid, True, "t", self.root))
        self.act("ask", request="do it again")
        self.assertNotIn("Type MERGE to merge anyway",
                         decisions.card_html(self.meta, self.jid, True, "t", self.root))

    def test_the_ask_for_changes_box_is_a_textarea(self):
        html = decisions.card_html(self.meta, self.jid, True, "t", self.root)
        self.assertIn('<textarea name="request"', html)

    def test_the_decision_text_cannot_inject_html(self):
        self.act("ask", request="<script>alert(1)</script>")
        html = decisions.card_html(self.meta, self.jid, True, "t", self.root)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)


# ------------------------------------------------------------------ the real pages

class Pages(unittest.TestCase):
    JID = "20261112-101112-ab12"

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="jb08-web-"))
        cls.saved = (jobboard.JOBS, jobboard.WORK, jobboard.PORT, jobboard.READONLY)
        jobboard.JOBS = cls.tmp / "jobs"
        jobboard.WORK = cls.tmp / "work"
        jobboard.JOBS.mkdir(parents=True)
        cls.secret = cls.tmp / "state" / "human-secret"
        cls.p_secret = mock.patch.object(decisions, "SECRET_FILE", cls.secret)
        cls.p_secret.start()
        cls.p_host = mock.patch.object(upgrades, "HOST_MODE", True)
        cls.p_host.start()
        cls.root = jobboard.WORK / "u08proj"
        cls.base = make_repo(cls.root, cls.JID, "agent/" + cls.JID)
        s = socket.socket(); s.bind(("127.0.0.1", 0)); cls.port = s.getsockname()[1]; s.close()
        jobboard.PORT = cls.port                       # guard() reads this at request time
        jobboard.READONLY = False
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), jobboard.H)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close(); cls.thr.join(timeout=5)
        cls.p_host.stop(); cls.p_secret.stop()
        (jobboard.JOBS, jobboard.WORK, jobboard.PORT, jobboard.READONLY) = cls.saved
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        decisions._hint_cache.clear()
        for d in jobboard.JOBS.iterdir():
            shutil.rmtree(d, ignore_errors=True)
        (jobboard.JOBS / self.JID).mkdir(parents=True)
        shutil.rmtree(self.root, ignore_errors=True)      # one test's merges are not another's
        self.base = make_repo(self.root, self.JID, "agent/" + self.JID)
        self.meta = meta_of(self.JID, folder="u08proj", base=self.base,
                            branch_start="main", verify=("passed",))
        jobboard.save_meta(self.meta, self.JID)

    def record(self):
        return jobboard.load_meta(self.JID)

    def head(self, branch=None):
        return git(self.root, "rev-parse", branch or "HEAD").strip()

    def fetch(self, path, data=None, cookies="", origin=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body)
        req.add_header("Cookie", f"jb={jobboard.TOKEN}" + (f"; {cookies}" if cookies else ""))
        if origin:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers

    def session(self):
        """Open the job page the way a browser does: it hands back the decision session."""
        code, page, hdrs = self.fetch(f"/job/{self.JID}")
        self.assertEqual(code, 200)
        sids = [v.split("=", 1)[1].split(";")[0] for v in (hdrs.get_all("Set-Cookie") or [])
                if v.startswith("aj=")]
        toks = re.findall(r'name="ajtok" value="([^"]+)"', page)
        return (sids[0] if sids else ""), (toks[0] if toks else ""), page

    def press(self, act, sid="", tok="", **fields):
        form = {"act": act, decisions.TOKEN_FIELD: tok}
        form.update(fields)
        return self.fetch(f"/decide/{self.JID}", form, cookies=f"aj={sid}" if sid else "")

    # ---- the page

    def test_the_job_page_carries_the_card_and_a_session_of_its_own(self):
        sid, tok, page = self.session()
        self.assertTrue(sid and tok)
        self.assertIn("<h2>Approve or reject</h2>", page)
        self.assertIn("<button>Merge into main</button>", page)
        self.assertIn("Ask for changes", page)
        self.assertIn('<button class="ghost">Reject</button>', page)
        self.assertNotIn(str(decisions.secret()), page)          # only its HMAC goes out

    def test_the_session_cookie_is_HttpOnly_and_kept_on_a_second_visit(self):
        sid, _tok, _page = self.session()
        code, page, hdrs = self.fetch(f"/job/{self.JID}", cookies=f"aj={sid}")
        self.assertEqual(code, 200)
        self.assertFalse([v for v in (hdrs.get_all("Set-Cookie") or []) if v.startswith("aj=")])
        self.assertIn(f'name="ajtok" value="{decisions.token_for(sid)}"', page)

    # ---- the secret gate

    def test_a_press_without_the_token_is_refused_and_changes_nothing(self):
        head = self.head("main")
        for cookies, tok in (("", ""), (f"aj={decisions.new_sid()}", ""),
                             ("", decisions.token_for(decisions.new_sid()))):
            code, _body, _h = self.press("merge", tok=tok, )
            self.assertEqual(code, 403, (cookies, tok))
        self.assertEqual(self.head("main"), head)
        self.assertIsNone(self.record().get("decision_log"))

    def test_a_token_from_another_session_is_refused(self):
        _sid, _tok, _page = self.session()
        code, _b, _h = self.press("merge", sid=decisions.new_sid(),
                                  tok=decisions.token_for(decisions.new_sid()))
        self.assertEqual(code, 403)
        self.assertEqual(self.head("main"), self.base)

    def test_a_page_that_never_ran_the_button_cannot_post_it(self):
        sid, tok, _page = self.session()
        code, _b, _h = self.press("merge", sid=sid, tok="0" * 64)
        self.assertEqual(code, 403)

    def test_a_get_to_the_decision_route_is_not_a_decision(self):
        code, _b, _h = self.fetch(f"/decide/{self.JID}")
        self.assertEqual(code, 404)

    def test_a_forged_body_from_a_script_on_this_machine_is_refused(self):
        """What requirement 6 is about: a job on the same PC knows the URL and the record,
        but has no session cookie and cannot make the token."""
        body = urllib.parse.urlencode({"act": "merge", decisions.TOKEN_FIELD: decisions.secret()}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/decide/{self.JID}", data=body)
        req.add_header("Cookie", f"jb={jobboard.TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 403)
        self.assertEqual(self.head("main"), self.base)

    # ---- pressing the buttons

    def test_approve_and_merge_merges_locally_and_says_not_pushed(self):
        sid, tok, _page = self.session()
        code, _b, _h = self.press("merge", sid=sid, tok=tok)
        self.assertEqual(code, 200)
        self.assertEqual(self.head("main"), self.head("agent/" + self.JID))
        m = self.record()
        self.assertEqual(m["decision"], "merged")
        self.assertEqual(m["decision_log"][-1]["how"], "fast-forward")
        self.assertFalse(m["decision_log"][-1]["pushed"])
        self.assertIn("merged", [e["type"] for e in timeline(jobboard.job_dir(self.JID))])
        _c2, page, _h2 = self.fetch(f"/job/{self.JID}", cookies=f"aj={sid}")
        self.assertIn("Not pushed", page)

    def test_the_merge_button_names_the_branch_and_a_moved_main_gets_a_merge_commit(self):
        git(self.root, "checkout", "-q", "main")
        (self.root / "a.txt").write_text("one\nmain's own\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "main moved on")
        git(self.root, "checkout", "-q", "agent/" + self.JID)
        sid, tok, _page = self.session()
        self.press("merge", sid=sid, tok=tok)
        self.assertIn("Merge agent job %s: Add the thing to the app" % self.JID,
                      git(self.root, "log", "-1", "--format=%s", "main"))

    def test_failed_checks_leave_the_button_grey_until_MERGE_is_typed(self):
        jobboard.update_meta(self.JID, verify={"status": "failed"})
        sid, tok, page = self.session()
        self.assertIn("<button disabled", page)
        self.press("merge", sid=sid, tok=tok)
        self.assertEqual(self.head("main"), self.base)
        self.assertIn("Checks didn't pass", self.record()["decision_log"][-1]["reason"])
        self.press("merge", sid=sid, tok=tok, typed="MERGE")
        self.assertEqual(self.head("main"), self.head("agent/" + self.JID))
        self.assertEqual(self.record()["decision"], "merged")

    def test_conflict_leaves_the_repo_alone_and_offers_the_fix_job(self):
        git(self.root, "checkout", "-q", "agent/" + self.JID)
        (self.root / "a.txt").write_text("job's line\n")
        git(self.root, "commit", "-qam", "job changed a.txt")
        git(self.root, "checkout", "-q", "main")
        (self.root / "a.txt").write_text("main's line\n")
        git(self.root, "commit", "-qam", "main changed a.txt")
        head = self.head("main")
        git(self.root, "checkout", "-q", "agent/" + self.JID)
        sid, tok, _page = self.session()
        self.press("merge", sid=sid, tok=tok)
        self.assertEqual(self.head("main"), head)
        self.assertEqual(git(self.root, "status", "--porcelain").strip(), "")
        _c, page, _h = self.fetch(f"/job/{self.JID}", cookies=f"aj={sid}")
        self.assertIn("Ask Qwen to fix the conflict", page)
        self.press("fix-conflict", sid=sid, tok=tok)
        jobs = [m for _, m in jobboard.all_job_dirs() if m["id"] != self.JID]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["parent"], self.JID)
        self.assertIn("Resolve every conflict", jobs[0]["job"])

    def test_undo_merge_brings_main_back(self):
        sid, tok, _page = self.session()
        self.press("merge", sid=sid, tok=tok)
        self.press("undo-merge", sid=sid, tok=tok)
        self.assertEqual(self.head("main"), self.base)
        self.assertEqual(self.record()["decision"], "merge-undone")

    def test_reject_renames_the_branch_and_undo_puts_it_back(self):
        sid, tok, _page = self.session()
        self.press("reject", sid=sid, tok=tok)
        self.assertIn("agent/rejected/%s" % self.JID, git(self.root, "branch"))
        self.assertTrue((self.root / "feature.py").exists())
        self.press("undo-reject", sid=sid, tok=tok)
        self.assertIn("agent/%s" % self.JID, git(self.root, "branch"))

    def test_ask_for_changes_queues_a_follow_up_on_the_same_folder(self):
        jobboard.update_meta(self.JID, session="sess-12345678")
        sid, tok, _page = self.session()
        self.press("ask", sid=sid, tok=tok, request="Make the button blue.")
        jobs = [m for _, m in jobboard.all_job_dirs() if m["id"] != self.JID]
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]["parent"], jobs[0]["folder"], jobs[0]["resume"]),
                         (self.JID, "u08proj", "sess-12345678"))
        self.assertIn("Make the button blue.", jobs[0]["job"])

    def test_a_record_the_worker_would_not_start_can_still_be_decided(self):
        """The bug the 8091 run caught: the decision route used jobboard.check_record - the
        worker's "could I launch this job" check - so a finished job whose record fails it for
        an unrelated reason (its parent job has since been deleted) swallowed the press: no
        merge, no word, nothing in the record."""
        jobboard.update_meta(self.JID, parent="20260101-000000-ffff")
        self.assertEqual(jobboard.check_record(self.JID, self.record()), "parent")
        sid, tok, _page = self.session()
        self.press("merge", sid=sid, tok=tok)
        self.assertEqual(self.head("main"), self.head("agent/" + self.JID))
        self.assertEqual(self.record()["decision"], "merged")

    def test_a_press_the_board_will_not_take_says_so_on_the_page(self):
        sid, tok, _page = self.session()
        jobboard.update_meta(self.JID, status="running")
        code, page, _h = self.press("merge", sid=sid, tok=tok)
        self.assertEqual(code, 200)
        self.assertIn("The job has to finish before you can decide.", page)
        self.assertIsNone(self.record().get("decision_log"))
        self.assertEqual(self.head("main"), self.base)

    def test_a_job_that_is_still_running_cannot_be_decided(self):
        sid, tok, _page = self.session()          # a valid session from the finished page
        jobboard.update_meta(self.JID, status="running")
        code, _b, _h = self.press("merge", sid=sid, tok=tok)
        self.assertEqual(code, 200)                     # back to the job page, nothing done
        self.assertIsNone(self.record().get("decision_log"))
        self.assertEqual(self.head("main"), self.base)

    def test_a_running_job_page_has_no_decide_card(self):
        jobboard.update_meta(self.JID, status="running")
        _sid, _tok, page = self.session()
        self.assertNotIn("<h2>Approve or reject</h2>", page)

    def test_a_record_that_no_longer_matches_the_repo_is_refused_not_a_broken_page(self):
        sid, tok, _page = self.session()
        jobboard.update_meta(self.JID, decision_log=[
            {"kind": "merge", "time": 1000.0, "target": "a-branch-that-is-gone",
             "branch": "agent/gone", "how": "fast-forward", "before": "abc1234", "after": "def5678"}])
        code, _b, _h = self.press("undo-merge", sid=sid, tok=tok)
        self.assertEqual(code, 200)
        last = self.record()["decision_log"][-1]
        self.assertEqual(last["kind"], "refused")
        self.assertIn("a-branch-that-is-gone", last["reason"])
        _c, page, _h2 = self.fetch(f"/job/{self.JID}", cookies=f"aj={sid}")
        self.assertIn("That decision could not be made", page)

    def test_the_job_page_needs_the_login_cookie_as_ever(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/job/{self.JID}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertIn(code, (401, 403))


if __name__ == "__main__":
    sys.exit(unittest.main())
