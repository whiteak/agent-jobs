#!/usr/bin/env python3
r"""Upgrade 7b: the Changes tab (diff viewer) and the check badges.

* Parser   - agentjobs_upgrades.diffparse on hand-written git output: added, deleted,
             renamed, binary markers, "\ No newline at end of file", generated paths,
             and the side-by-side pairing.
* Collect  - diffview.py against a REAL temp git repo (modified/added/deleted/renamed/
             binary/generated/untracked files, a checkpoint commit), through SafeGit with
             no wrapper: git runs on the temp folder, never in podman.
* Pages    - the real handler served in-process on its own port (jobboard.PORT set, like
             tests/test_jobweb_bench.py): /api/changes, /api/changes/<jid>/file,
             /api/check, the badge markup in the list and in /api/statuses, the card and
             its script on the job page (node --check), login guard, bad refs and paths.

Nothing here calls the model server (127.0.0.1:8080) or starts an agent.
"""
import base64
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
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

if "jobboard" not in sys.modules:      # standalone run: same isolated env as test_jobstatus
    _hb = tempfile.mkdtemp(prefix="jb7b-home-")
    os.environ.update({"HOME": _hb, "JOBBOARD_JOBS": os.path.join(_hb, "jobs"),
                       "JOBBOARD_WORK": os.path.join(_hb, "work"), "JOBBOARD_READONLY": "1"})
import jobboard  # noqa: E402
import upgrades  # noqa: E402
import badges  # noqa: E402
import diffview  # noqa: E402
from agentjobs_upgrades import diffparse  # noqa: E402
from agentjobs_upgrades.safegit import SafeGit  # noqa: E402


def git(repo, *a, env=None):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t",
                           "-c", "user.email=t@t", *a], cwd=str(repo), check=True,
                          capture_output=True, text=True, env=env).stdout


def checkpoint_commit(repo, idxfile, message="checkpoint snapshot"):
    """A checkpoint commit the way CheckpointManager makes one: its own index file, so
    the job's index, work tree and branch stay exactly as they were."""
    env = dict(os.environ, GIT_INDEX_FILE=str(idxfile))
    head = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "read-tree", head, env=env)
    git(repo, "add", "-A", ".", env=env)
    tree = git(repo, "write-tree", env=env).strip()
    return git(repo, "commit-tree", tree, "-p", head, "-m", message, env=env).strip()


# ------------------------------------------------------------------ the parser

ADDED = """diff --git a/new.txt b/new.txt
new file mode 100644
index 0000000..7c536c3
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+one
+two
"""

DELETED = """diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index 7c536c3..0000000
--- a/gone.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-one
-two
"""

RENAMED = """diff --git a/old.py b/src/new.py
similarity index 76%
rename from old.py
rename to src/new.py
index 3fc6d0b..8b1ad0b 100644
--- a/old.py
+++ b/src/new.py
@@ -1,3 +1,3 @@
 keep
-old line
+new line
 keep
"""

BINARY = """diff --git a/logo.png b/logo.png
index 3b1f2c7..9c0a1b2 100644
Binary files a/logo.png and b/logo.png differ
"""

NO_EOL = """diff --git a/t.txt b/t.txt
index 1111111..2222222 100644
--- a/t.txt
+++ b/t.txt
@@ -1,2 +1,2 @@
 keep
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""


class Parser(unittest.TestCase):
    def one(self, text):
        files = diffparse.parse(text)
        self.assertEqual(len(files), 1)
        return files[0]

    def test_added_file(self):
        f = self.one(ADDED)
        self.assertEqual((f.path, f.status, f.added, f.removed), ("new.txt", "added", 2, 0))
        self.assertEqual([(k, o, n) for (k, o, n, _) in f.hunks[0].lines],
                         [("+", None, 1), ("+", None, 2)])

    def test_deleted_file(self):
        f = self.one(DELETED)
        self.assertEqual((f.path, f.status, f.added, f.removed), ("gone.txt", "deleted", 0, 2))
        self.assertEqual([(k, o, n) for (k, o, n, _) in f.hunks[0].lines],
                         [("-", 1, None), ("-", 2, None)])

    def test_renamed_file_with_changes(self):
        f = self.one(RENAMED)
        self.assertEqual((f.old_path, f.path, f.status), ("old.py", "src/new.py", "renamed"))
        self.assertEqual((f.added, f.removed), (1, 1))
        self.assertEqual([k for (k, _, _, _) in f.hunks[0].lines], [" ", "-", "+", " "])

    def test_binary_marker(self):
        f = self.one(BINARY)
        self.assertTrue(f.binary)
        self.assertEqual((f.added, f.removed), (0, 0))
        self.assertEqual(f.hunks, [])

    def test_no_newline_at_end_of_file(self):
        f = self.one(NO_EOL)
        kinds = [(k, t) for (k, _, _, t) in f.hunks[0].lines]
        self.assertIn(("\\", "No newline at end of file"), kinds)
        self.assertEqual((f.added, f.removed), (1, 1))          # the marker is not a line

    def test_several_files_and_summary(self):
        files = diffparse.parse(ADDED + BINARY + RENAMED)
        self.assertEqual([f.path for f in files], ["new.txt", "logo.png", "src/new.py"])
        s = diffparse.summary(files)
        self.assertEqual([(x["path"], x["status"], x["binary"]) for x in s],
                         [("new.txt", "added", False), ("logo.png", "modified", True),
                          ("src/new.py", "renamed", False)])

    def test_generated_paths(self):
        for p in ("build/app.apk", "node_modules/left-pad/index.js", "package-lock.json",
                  "app/build/outputs/a.apk", "web/__pycache__/x.pyc", "assets/logo.png"):
            self.assertTrue(diffparse.is_generated(p), p)
        for p in ("web/jobboard.py", "src/app.py", "README.md", "gradlew", "tests/test_a.py"):
            self.assertFalse(diffparse.is_generated(p), p)

    def test_side_by_side_pairs_changes_and_keeps_gaps(self):
        f = self.one(RENAMED)
        self.assertEqual(diffparse.side_by_side(f.hunks[0]),
                         [((1, " ", "keep"), (1, " ", "keep")),
                          ((2, "-", "old line"), (2, "+", "new line")),
                          ((3, " ", "keep"), (3, " ", "keep"))])
        f = self.one(ADDED)
        self.assertEqual(diffparse.side_by_side(f.hunks[0]),
                         [(None, (1, "+", "one")), (None, (2, "+", "two"))])


# ------------------------------------------------------------------ a real repo, collected

def make_repo(tmp):
    """A project with one of everything, and three commits after the base."""
    p = Path(tmp) / "proj"
    (p / "src").mkdir(parents=True)
    (p / "src" / "a.py").write_text("one\ntwo\nthree\n")
    (p / "src" / "gone.py").write_text("old\n")
    (p / "src" / "old_name.py").write_text("keep me\n")
    (p / "logo.png").write_bytes(b"\x89PNG\x00\x01old")
    (p / "build" / "gen.txt").parent.mkdir()
    (p / "build" / "gen.txt").write_text("made\n")
    git(p, "init", "-q", "-b", "main")
    git(p, "add", "-A")
    git(p, "commit", "-qm", "base")
    base = git(p, "rev-parse", "HEAD").strip()
    # commit 1: modify, delete, rename, a binary change, a generated change, add a file
    (p / "src" / "a.py").write_text("one\nTWO\nthree\nfour\n")
    (p / "src" / "new_name.py").write_text("keep me\n")
    (p / "src" / "note.md").write_text("a note\nsecond\n")
    git(p, "rm", "-q", "src/gone.py")
    git(p, "add", "-A", "src/a.py", "src/note.md")
    git(p, "mv", "-f", "src/old_name.py", "src/new_name.py")
    (p / "logo.png").write_bytes(b"\x89PNG\x00\x02newer bytes")
    (p / "build" / "gen.txt").write_text("made\nmore\n")
    git(p, "add", "-A")
    git(p, "commit", "-qm", "did the thing")
    # commit 2: no new line at the end of a file
    (p / "src" / "note.md").write_text("a note\nno newline at the end")
    git(p, "add", "-A")
    git(p, "commit", "-qm", "cut the newline")
    # ... plus work in progress the job never committed
    (p / "src" / "scratch.py").write_text("print(1)\n")
    (p / "src" / "a.py").write_text("one\nTWO\nthree\nfour\nfive\n")
    return p, base


def meta_of(jid, folder, base, **kw):
    now = time.time()
    m = {"id": jid, "kind": "job", "mode": "local-claude", "folder": folder, "job": "do it",
         "offline": False, "status": "done", "created": now - 600, "started": now - 600,
         "finished": now - 60, "exit": 0, "resume": None, "parent": None, "session": None,
         "git_start": base}
    m.update(kw)
    return m


class Collect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="jb7b-"))
        cls.repo, cls.base = make_repo(cls.tmp)
        # a checkpoint commit, exactly like CheckpointManager makes one (own index file:
        # the job's index, work tree and branch stay as the job left them)
        cls.ckpt = checkpoint_commit(cls.repo, cls.tmp / "ckpt-index")

    def setUp(self):
        diffview._cache.clear()
        self.git = SafeGit(self.repo)          # no wrap: git runs on the temp folder
        self.m = meta_of("u07b-collect", "proj", self.base,
                         checkpoints={"checkpoints": [{"n": 1, "step": "first step", "commit": self.ckpt,
                                                       "passed": 2, "failed": 0, "build_ok": True,
                                                       "time": time.time()}],
                                      "rollbacks": 0, "discarded": []})

    def view(self, ref="all"):
        return diffview.summary(self.m, self.git, ref)

    def test_file_list_with_counts_and_states(self):
        d = self.view()
        self.assertTrue(d["ok"], d)
        by = {f["path"]: f for f in d["files"]}
        self.assertEqual(by["src/a.py"]["status"], "modified")
        self.assertEqual((by["src/a.py"]["added"], by["src/a.py"]["removed"]), (3, 1))   # base -> work tree
        self.assertEqual(by["src/gone.py"]["status"], "deleted")
        self.assertEqual(by["src/note.md"]["status"], "added")
        self.assertEqual((by["src/new_name.py"]["status"], by["src/new_name.py"]["old"]),
                         ("renamed", "src/old_name.py"))
        self.assertEqual(by["src/scratch.py"]["status"], "untracked")     # never committed
        self.assertEqual(by["src/scratch.py"]["added"], 1)
        self.assertEqual(d["how"], "everything since %s (where the job started), uncommitted included"
                         % self.base[:8])

    def test_binary_and_generated_are_listed_but_not_shown(self):
        d = self.view()
        by = {f["path"]: f for f in d["files"]}
        self.assertTrue(by["logo.png"]["binary"])
        self.assertFalse(by["logo.png"]["shown"])
        self.assertEqual(by["logo.png"]["why"], "binary")
        self.assertFalse(by["build/gen.txt"]["shown"])      # diffparse.is_generated
        self.assertEqual(by["build/gen.txt"]["why"], "generated")
        # and the endpoint refuses to show them too
        d2 = diffview.file_diff(self.m, self.git, "all", "logo.png")
        self.assertFalse(d2["shown"])
        self.assertEqual(d2["why"], "binary")
        d3 = diffview.file_diff(self.m, self.git, "all", "build/gen.txt")
        self.assertEqual(d3["why"], "generated")

    def test_the_view_picker_lists_commits_and_checkpoints(self):
        d = self.view()
        kinds = {r["kind"] for r in d["refs"]}
        self.assertEqual(kinds, {"all", "commit", "checkpoint"})
        labels = " ".join(r["label"] for r in d["refs"])
        self.assertIn("did the thing", labels)
        self.assertIn("cut the newline", labels)
        self.assertIn("Checkpoint 1 first step", labels)
        self.assertNotIn("checkpoint snapshot", labels)      # the checkpoint commit is a checkpoint, not a job commit

    def test_one_commit_shows_only_its_changes(self):
        ref = next(r["ref"] for r in self.view()["refs"] if "did the thing" in r["label"])
        d = self.view(ref)
        self.assertIn("commit", d["how"])
        by = {f["path"]: f for f in d["files"]}
        self.assertIn("src/note.md", by)                      # added in that commit
        self.assertNotIn("src/scratch.py", by)                # untracked: not in a commit
        self.assertEqual(by["src/a.py"]["added"], 2)          # only what that commit did

    def test_a_checkpoint_view_shows_changes_up_to_the_checkpoint(self):
        d = self.view("k:1")
        self.assertIn("checkpoint 1", d["how"])
        self.assertIn("src/scratch.py", {f["path"] for f in d["files"]})   # snapshotted into the checkpoint
        self.assertNotIn("cut the newline", d["how"])
        bad = self.view("k:9")
        self.assertFalse(bad["ok"])

    def test_file_diff_rows(self):
        d = diffview.file_diff(self.m, self.git, "all", "src/a.py")
        self.assertEqual(d["status"], "modified")
        self.assertEqual((d["added"], d["removed"]), (3, 1))
        rows = d["hunks"][0]["lines"]
        self.assertEqual([r[0] for r in rows].count("+"), 3)
        self.assertEqual([r[0] for r in rows].count("-"), 1)
        self.assertTrue(all(len(r) == 4 for r in rows))                      # kind, old no, new no, text
        self.assertEqual(d["hunks"][0]["pair"],
                         [[[1, " ", "one"], [1, " ", "one"]],
                          [[2, "-", "two"], [2, "+", "TWO"]],
                          [[3, " ", "three"], [3, " ", "three"]],
                          [None, [4, "+", "four"]],
                          [None, [5, "+", "five"]]])

    def test_no_newline_marker_survives_to_the_page(self):
        d = diffview.file_diff(self.m, self.git, "all", "src/note.md")
        kinds = [r[0] for h in d["hunks"] for r in h["lines"]]
        self.assertIn("nl", kinds)                    # the JS renders it as "\ No newline at end of file"

    def test_untracked_file_diff(self):
        d = diffview.file_diff(self.m, self.git, "all", "src/scratch.py")
        self.assertEqual(d["status"], "added")
        self.assertEqual(d["hunks"][0]["lines"], [["+", None, 1, "print(1)"]])

    def test_deleted_and_renamed_files(self):
        gone = diffview.file_diff(self.m, self.git, "all", "src/gone.py")
        self.assertEqual(gone["status"], "deleted")
        self.assertEqual(gone["removed"], 1)
        ren = diffview.file_diff(self.m, self.git, "all", "src/new_name.py")
        self.assertEqual(ren["status"], "renamed")
        self.assertEqual(ren["old"], "src/old_name.py")

    def test_refs_and_paths_from_the_browser_are_checked(self):
        self.assertEqual(diffview.file_diff(self.m, self.git, "c:" + "0" * 40, "src/a.py")["error"],
                         "unknown revision")
        for p in ("../outside.txt", "/etc/passwd", "src/../src/a.py", ".git/config", ""):
            self.assertEqual(diffview.file_diff(self.m, self.git, "all", p)["error"], "bad path", p)
        # a sha that is not one of this job's commits is refused even though the repo has it
        # (unreachable from HEAD, so it cannot be one of the job's commits either)
        old = time.gmtime(self.m["created"] - 4000)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", old)
        tree = git(self.repo, "rev-parse", self.base + "^{tree}").strip()
        outside = git(self.repo, "commit-tree", tree, "-p", self.base, "-m", "before the job",
                      env=dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)).strip()
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", outside))
        self.assertEqual(diffview.summary(self.m, self.git, "c:" + outside).get("ok"), False)
        self.assertEqual(diffview.file_diff(self.m, self.git, "c:" + outside, "src/a.py")["error"],
                         "unknown revision")

    def test_only_the_dot_git_folder_is_a_bad_path_not_every_dot_git_name(self):
        """# U07b review: ".gitignore", ".gitattributes" and ".github/workflows/x.yml" are
        ordinary project files. Only .git itself (and what is inside it) is off limits."""
        for p in (".gitignore", ".gitattributes", ".github/workflows/ci.yml", ".gitlab-ci.yml",
                  "src/.gitkeep", "a/.gitlab-ci.yml"):
            self.assertFalse(diffview.bad_path(p), p)
        for p in (".git", ".git/", ".git/config", ".git//config", "src/.git/hooks/pre-commit"):
            self.assertTrue(diffview.bad_path(p), p)
        tmp = Path(tempfile.mkdtemp(prefix="jb7b-dot-"))
        repo, base = make_repo(tmp)
        want = {".gitignore": "*.pyc", ".gitattributes": "*.txt text",
                ".github/workflows/ci.yml": "jobs: none"}
        for rel, text in want.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text + "\n")
        m, g = meta_of("u07b-dot", "proj", base), SafeGit(repo)
        by = {f["path"]: f for f in diffview.summary(m, g, "all")["files"]}
        for rel, text in want.items():
            self.assertIn(rel, by, rel)                        # listed like any other file
            self.assertTrue(by[rel]["shown"], by.get(rel))     # and not hidden
            d = diffview.file_diff(m, g, "all", rel)
            self.assertNotEqual(d.get("error"), "bad path", rel)
            self.assertTrue(d.get("shown"), d)
            self.assertEqual(d["hunks"][0]["lines"], [["+", None, 1, text]], rel)
        # the folder itself stays refused, however it is spelled or reached
        for p in (".git", ".git/", ".git/config", ".git/refs/heads/main",
                  "src/../.git/config", ".git//config"):
            self.assertEqual(diffview.file_diff(m, g, "all", p)["error"], "bad path", p)

    def test_huge_diff_is_cut_but_still_usable(self):
        """10,000+ changed lines: the JSON stays bounded and the page can still render it
        (the script inserts CHUNK rows at a time)."""
        tmp = Path(tempfile.mkdtemp(prefix="jb7b-big-"))
        repo, base = make_repo(tmp)
        (repo / "big.txt").write_text("".join(f"line {i} of the diff\n" for i in range(15000)))
        d = diffview.file_diff(meta_of("u07b-big", "proj", base), SafeGit(repo), "all", "big.txt")
        self.assertTrue(d["shown"])
        self.assertTrue(d["truncated"])
        rows = sum(len(h["lines"]) for h in d["hunks"])
        self.assertLessEqual(rows, diffview.DIFF_MAX_LINES)
        self.assertGreater(rows, 10000)
        s = diffview.summary(meta_of("u07b-big2", "proj", base), SafeGit(repo), "all")
        big = next(f for f in s["files"] if f["path"] == "big.txt")
        self.assertEqual((big["added"], big["shown"]), (15000, True))
        self.assertGreater(s["totals"]["added"], 15000)

    def test_an_untracked_file_too_big_to_measure_is_listed_without_a_made_up_count(self):
        tmp = Path(tempfile.mkdtemp(prefix="jb7b-huge-"))
        repo, base = make_repo(tmp)
        (repo / "huge.txt").write_text("x" * (diffview.UNTRACKED_READ + 10))
        m, g = meta_of("u07b-huge", "proj", base), SafeGit(repo)
        f = next(x for x in diffview.summary(m, g, "all")["files"] if x["path"] == "huge.txt")
        self.assertTrue(f["shown"])
        self.assertEqual((f["added"], f["removed"]), (0, 0))     # not measured, not "+1"
        self.assertEqual(f["why"], "too big to count")
        d = diffview.file_diff(m, g, "all", "huge.txt")
        self.assertTrue(d["shown"] and d["truncated"])           # the diff is cut, not endless
        self.assertLessEqual(sum(len(h["lines"]) for h in d["hunks"]), diffview.DIFF_MAX_LINES)

    def test_a_folder_that_is_not_a_git_repo_says_so(self):
        plain = Path(tempfile.mkdtemp(prefix="jb7b-plain-"))
        (plain / "x.txt").write_text("hi\n")
        d = diffview.summary(meta_of("u07b-nogit", "plain", ""), SafeGit(plain), "all")
        self.assertEqual(d, {"ok": False, "error": "not a git repository"})

    def test_results_are_cached_but_failures_are_not(self):
        """A running job refreshes every 10 s: the jail call must not run twice that often,
        but a failed one must be retried (press Refresh and it tries again)."""
        calls, real = [], diffview.sh

        def counting(git_, script):
            calls.append(script.strip()[:14])
            return real(git_, script)

        with mock.patch.object(diffview, "sh", counting):
            self.assertTrue(diffview.summary(self.m, self.git, "all")["ok"])
            first = len(calls)
            self.assertEqual(first, 2, "one probe call and one list call")
            self.assertTrue(diffview.summary(self.m, self.git, "all")["ok"])
            self.assertEqual(len(calls), first, "the second read comes from the cache")
            # a folder that is not a repo fails, and that failure is retried next time
            plain = Path(tempfile.mkdtemp(prefix="jb7b-cache-"))
            (plain / "x.txt").write_text("hi\n")
            m2, g2 = meta_of("u07b-cache", "plain", ""), SafeGit(plain)
            self.assertFalse(diffview.summary(m2, g2, "all")["ok"])
            n1 = len(calls)
            self.assertFalse(diffview.summary(m2, g2, "all")["ok"])
            self.assertGreater(len(calls), n1, "a failure is never cached")


# ------------------------------------------------------------------ badges

class CheckBadges(unittest.TestCase):
    def test_one_badge_per_check_with_counts(self):
        m = {"verify": {"rounds": [{"round": 0, "summary": {"status": "failed", "results": [
            {"name": "build", "cmd": "./gradlew assembleDebug", "ok": True, "exit_code": 0},
            {"name": "tests", "cmd": "pytest", "ok": False, "exit_code": 1,
             "passed": 41, "failed": 1, "output_file": "/x/check-tests.log"}]}}]}}
        self.assertEqual([(b["text"], b["ok"], b["href"]) for b in badges.badges(m, "j9")],
                         [("build", True, "/api/check/j9?src=round0&n=0"),
                          ("tests 41/42", False, "/api/check/j9?src=round0&n=1")])

    def test_a_later_recheck_wins(self):
        m = {"verify": {"rounds": [{"round": 0, "summary": {"results": [{"name": "old", "ok": False}]}}]},
             "recheck": {"summary": {"results": [{"name": "web tests", "ok": True, "passed": 346}]}}}
        self.assertEqual([b["text"] for b in badges.badges(m)], ["web tests 346/346"])
        self.assertEqual(badges.source(m)[0], "recheck")

    def test_no_checks_no_badges(self):
        self.assertEqual(badges.badges({"status": "done"}), [])
        self.assertEqual(badges.badges_html({"status": "done"}), "")

    def test_html_is_escaped(self):
        m = {"recheck": {"summary": {"results": [{"name": '<img src=x>', "cmd": 'a"b', "ok": True}]}}}
        h = badges.badges_html(m, "j1")
        self.assertIn("&lt;img", h)
        self.assertNotIn("<img", h)

    def test_output_only_from_inside_the_job_folder(self):
        root = Path(tempfile.mkdtemp(prefix="jb7b-out-"))
        (root / "checks" / "round-0").mkdir(parents=True)
        (root / "checks" / "round-0" / "check-tests.log").write_text("$ pytest\nFAILED\n" * 5)
        outside = root.parent / "elsewhere.log"
        outside.write_text("secret\n")
        m = {"verify": {"rounds": [{"round": 0, "summary": {"results": [
            {"name": "tests", "output_file": str(root / "checks" / "round-0" / "check-tests.log")},
            {"name": "sneaky", "output_file": str(outside)},
            {"name": "gone", "output_file": str(root / "checks" / "round-0" / "nope.log")},
            {"name": "trunc", "output_file": str(root / "checks" / "round-0" / "big.log")}]}}]}}
        (root / "checks" / "round-0" / "big.log").write_text("x" * (badges.MAX_BYTES + 1000))
        got = badges.check_text(m, "round0", 0, root)
        self.assertIn("FAILED", got[0])
        self.assertIsNone(badges.check_text(m, "round0", 1, root))     # outside JOBS/<id>
        self.assertIsNone(badges.check_text(m, "round0", 2, root))     # never written
        big = badges.check_text(m, "round0", 3, root)
        self.assertTrue(big[1] and len(big[0]) < badges.MAX_BYTES + 200)
        self.assertIsNone(badges.check_text(m, "round0", 9, root))
        self.assertIsNone(badges.check_text(m, "round7", 0, root))


# ------------------------------------------------------------------ the running board

class Pages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="jb7b-web-"))
        cls.saved = (jobboard.JOBS, jobboard.WORK, jobboard.PORT)
        jobboard.JOBS = cls.tmp / "jobs"
        jobboard.JOBS.mkdir()
        jobboard.WORK = cls.tmp / "work"
        (jobboard.WORK / "proj").mkdir(parents=True)
        cls.repo, cls.base = make_repo(jobboard.WORK)
        jid = "20261111-120000-ab12"
        m = meta_of(jid, "proj", cls.base, checkpoints={"checkpoints": [], "rollbacks": 0, "discarded": []})
        m["verify"] = {"status": "failed", "rounds": [{"round": 0, "summary": {"status": "failed", "results": [
            {"name": "build", "cmd": "make", "ok": True, "exit_code": 0, "seconds": 3.2,
             "output_file": str(jobboard.JOBS / jid / "checks" / "round-0" / "check-build.log")},
            {"name": "tests", "cmd": "make test", "ok": False, "exit_code": 1, "passed": 41, "failed": 1,
             "output_file": str(jobboard.JOBS / jid / "checks" / "round-0" / "check-tests.log")}]}}]}
        logdir = jobboard.JOBS / jid / "checks" / "round-0"
        logdir.mkdir(parents=True)
        (logdir / "check-build.log").write_text("$ make\nbuilt ok\n")
        (logdir / "check-tests.log").write_text("$ make test\nFAILED src/test_a.py\n")
        jobboard.save_meta(m, jid)
        cls.jid = jid
        s = socket.socket(); s.bind(("127.0.0.1", 0)); cls.port = s.getsockname()[1]; s.close()
        jobboard.PORT = cls.port                # guard() reads this at request time
        cls.host = mock.patch.object(upgrades, "HOST_MODE", True)   # git on the temp folder, no podman
        cls.host.start()
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), jobboard.H)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close(); cls.thr.join(timeout=5)
        cls.host.stop()
        jobboard.JOBS, jobboard.WORK, jobboard.PORT = cls.saved

    def get(self, path, cookie=True):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if cookie:
            req.add_header("Cookie", f"jb={jobboard.TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def test_changes_endpoint_lists_the_job_s_changes(self):
        code, body = self.get(f"/api/changes/{self.jid}")
        self.assertEqual(code, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        by = {f["path"]: f for f in d["files"]}
        self.assertEqual(by["src/a.py"]["added"], 3)
        self.assertEqual(by["logo.png"]["why"], "binary")
        self.assertEqual(by["build/gen.txt"]["why"], "generated")
        self.assertIn("uncommitted included", d["how"])
        self.assertEqual(d["totals"]["files"], len(d["files"]))

    def test_changes_endpoint_one_file(self):
        code, body = self.get(f"/api/changes/{self.jid}/file?path=src%2Fa.py")
        self.assertEqual(code, 200, body[:300])
        d = json.loads(body)
        self.assertEqual(d["status"], "modified")
        self.assertEqual([r[0] for r in d["hunks"][0]["lines"]].count("+"), 3)
        self.assertEqual(d["hunks"][0]["pair"][0], [[1, " ", "one"], [1, " ", "one"]])

    def test_changes_endpoint_refuses_strange_refs_and_paths(self):
        self.assertEqual(self.get(f"/api/changes/{self.jid}/file?path=..%2Fetc%2Fpasswd")[0], 404)
        self.assertEqual(self.get(f"/api/changes/{self.jid}/file?path=src%2Fa.py&ref=c:deadbeef")[0], 404)
        code, body = self.get(f"/api/changes/{self.jid}?ref=c:deadbeef")
        self.assertEqual(code, 200)                     # the card shows the reason
        self.assertFalse(json.loads(body)["ok"])

    def test_checkpoint_view_through_the_endpoint(self):
        d = json.loads(self.get(f"/api/changes/{self.jid}")[1])
        refs = [r for r in d["refs"] if r["kind"] == "commit"]
        one = next(r for r in refs if "did the thing" in r["label"])
        d2 = json.loads(self.get(f"/api/changes/{self.jid}?ref={one['ref']}")[1])
        self.assertTrue(d2["ok"])
        self.assertIn("did the thing", d2["how"])
        self.assertNotIn("src/scratch.py", {f["path"] for f in d2["files"]})

    def test_check_output_endpoint(self):
        code, body = self.get(f"/api/check/{self.jid}?src=round0&n=1")
        self.assertEqual(code, 200)
        self.assertIn("FAILED src/test_a.py", body)
        self.assertEqual(self.get(f"/api/check/{self.jid}?src=round0&n=99")[0], 404)
        self.assertEqual(self.get(f"/api/check/{self.jid}")[0], 404)

    def test_pages_need_the_login_cookie(self):
        for path in (f"/api/changes/{self.jid}", f"/api/changes/{self.jid}/file?path=src%2Fa.py",
                     f"/api/check/{self.jid}?src=round0&n=0"):
            self.assertIn(self.get(path, cookie=False)[0], (401, 403), path)
        self.assertEqual(self.get("/api/changes/no-such-job")[0], 404)

    def test_job_page_has_the_changes_card_and_badges(self):
        code, h = self.get(f"/job/{self.jid}")
        self.assertEqual(code, 200)
        for needle in ('id="changescard"', "<h2>Changes</h2>", 'id="chref"', "Side by side",
                       "Next change", 'id="chdiff"', "EventSource"):
            self.assertIn(needle, h)
        self.assertIn('href="/api/check/%s?src=round0&amp;n=1"' % self.jid, h)   # tests 41/42 badge
        self.assertIn("tests 41/42", h)
        self.assertIn("build", h)

    def test_list_rows_carry_the_badges(self):
        code, rows = self.get("/api/rows")
        self.assertEqual(code, 200)
        self.assertIn('class="chk"', rows)
        self.assertIn("tests 41/42", rows)
        self.assertIn("/api/check/", rows)
        code, js = self.get("/api/statuses")
        s = json.loads(js)["statuses"][self.jid]
        self.assertEqual([b["text"] for b in s["check_badges"]], ["build", "tests 41/42"])
        self.assertEqual([b["ok"] for b in s["check_badges"]], [True, False])

    SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)

    def test_job_page_scripts_parse_with_node(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed: cannot syntax-check the page scripts")
        for path in ("/", f"/job/{self.jid}"):
            code, html = self.get(path)
            self.assertEqual(code, 200, path)
            bodies = self.SCRIPT_RE.findall(html)
            self.assertGreaterEqual(len(bodies), 1)
            tmp = Path(tempfile.mkdtemp(prefix="jb7b-js-"))
            for n, body in enumerate(bodies):
                p = tmp / f"script{n}.js"
                p.write_text(body, encoding="utf-8")
                r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{path}: <script> #{n}:\n{r.stderr}")
            # and all of them together: classic <script> blocks share one global scope
            joined = tmp / "all-in-order.js"
            joined.write_text("\n".join(bodies), encoding="utf-8")
            r = subprocess.run(["node", "--check", str(joined)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{path}: all scripts joined:\n{r.stderr}")


if __name__ == "__main__":
    unittest.main()
