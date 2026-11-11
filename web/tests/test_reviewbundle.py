#!/usr/bin/env python3
"""Tests for reviewbundle.py (standard library only).

Run:  cd /work/jobboard-ui && python3 -m unittest discover -s tests -v

The git callable is the same shape jobboard.py passes in (a shell script run
inside the project folder, stdout back), here backed by a throwaway git repo
in /tmp. reviewbundle itself must never call subprocess - everything it reads
from the project goes through this callable.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import reviewbundle  # noqa: E402


def meta(**over):
    now = time.time()
    m = {"id": "j1", "mode": "local-claude", "folder": "repo", "job": "Do the thing",
         "offline": False, "status": "done", "created": now - 600, "started": now - 600,
         "finished": now - 60, "exit": 0, "resume": None, "parent": None, "session": None}
    m.update(over)
    return m


def gitof(folder):
    """Test stand-in for jobboard's in_sandbox git callable."""
    def g(script):
        r = subprocess.run(["bash", "-c", script], cwd=folder,
                           capture_output=True, text=True, timeout=60)
        return r.stdout.strip()
    return g


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def events_file(lines):
    d = tempfile.mkdtemp(prefix="rb-ev-")
    p = os.path.join(d, "events.jsonl")
    with open(p, "w") as f:
        for e in lines:
            f.write(json.dumps(e) + "\n")
    return p


def tooluse(tid, name, cmd):
    return {"type": "assistant", "message": {"content":
            [{"type": "tool_use", "id": tid, "name": name, "input": {"command": cmd}}]}}


def toolres(tid, text, err=False):
    return {"type": "user", "message": {"content":
            [{"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": err}]}}


def resultev(text="All done. The tests pass.", turns=7, ms=1234000):
    return {"type": "result", "result": text, "is_error": False,
            "num_turns": turns, "duration_ms": ms}


BT_RE = re.compile(r"- (PASS|FAIL) `([^`]+)`([^\n]*)\n```text\n(.*?)\n```", re.S)


def bt_items(bundle):
    """{(status, cmd): (trailing note, fenced block)} from the build/test section."""
    sec = bundle.split("## Build and test results", 1)[1]
    return {(st, cmd): (note, block) for st, cmd, note, block in BT_RE.findall(sec)}


# ------------------------------------------------------------------ repo fixture

class RepoCase(unittest.TestCase):
    """A throwaway git repo shaped like a finished job's project folder.

    start commit (with an existing AGENT_REPORT.md) -> backdated "early" commit
    (inside the git_start range, outside the job window) -> two commits made now
    (c1, c2) -> one backdated commit c3 (outside the window) -> one uncommitted
    edit + two untracked files. c2 touches a lockfile, a 500-line generated
    file, a 120-line test file and modifies AGENT_REPORT.md; c1 also renames a
    file with no content change.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rb-repo-")

        def run(*args, env=None):
            e = dict(os.environ)
            if env:
                e.update(env)
            subprocess.run(list(args), cwd=self.dir, check=True,
                           capture_output=True, text=True, env=e)

        self.gitsh = gitof(self.dir)
        run("git", "init", "-q")
        run("git", "config", "user.name", "Test")
        run("git", "config", "user.email", "test@example.com")
        write(os.path.join(self.dir, "a.py"), "line one\nline two\n")
        write(os.path.join(self.dir, "b.txt"), "b1\nb2\n")
        write(os.path.join(self.dir, "c_move.txt"), "movable content\n")
        write(os.path.join(self.dir, "AGENT_REPORT.md"), "STARTREP-0z1\nolder job section\n")
        run("git", "add", "-A")
        startd = f"@{int(time.time()) - 1200} +0000"  # history predates the job window
        run("git", "commit", "-q", "-m", "start",
            env={"GIT_AUTHOR_DATE": startd, "GIT_COMMITTER_DATE": startd})
        self.start = self.gitsh("git rev-parse HEAD")

        old = f"@{int(time.time()) - 3000} +0000"
        write(os.path.join(self.dir, "c_early.py"), "EARLYCOMMIT-3w9\n")
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "early",
            env={"GIT_AUTHOR_DATE": old, "GIT_COMMITTER_DATE": old})
        self.early = self.gitsh("git rev-parse HEAD")

        # commits inside the job window (made "now")
        with open(os.path.join(self.dir, "a.py"), "a") as f:
            f.write("C1CHANGE-5r2\n")
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "c1: change a")
        self.c1 = self.gitsh("git rev-parse HEAD")
        write(os.path.join(self.dir, "package-lock.json"), '{"name": "x", "lock": "LOCKMARKER-9x7"}\n')
        write(os.path.join(self.dir, "big.py"), "".join(f"GENERATED-9x7 line {i}\n" for i in range(500)))
        os.mkdir(os.path.join(self.dir, "tests"))
        write(os.path.join(self.dir, "tests", "test_a.py"),
              "".join(f"TESTCODE-4h7 line {i}\n" for i in range(120)))
        with open(os.path.join(self.dir, "AGENT_REPORT.md"), "a") as f:
            f.write("REPORTMARKER-4q2 this job added a section\n")
        run("git", "mv", "c_move.txt", "c_moved.txt")
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "c2: report and junk")
        self.c2 = self.gitsh("git rev-parse HEAD")

        # a commit OUTSIDE the job window (backdated an hour)
        write(os.path.join(self.dir, "c3.py"), "C3CHANGE-7d4\n")
        run("git", "add", "-A")
        out3 = f"@{int(time.time()) - 3600} +0000"
        run("git", "commit", "-q", "-m", "c3: later other work",
            env={"GIT_AUTHOR_DATE": out3, "GIT_COMMITTER_DATE": out3})
        self.c3 = self.gitsh("git rev-parse HEAD")

        with open(os.path.join(self.dir, "b.txt"), "a") as f:
            f.write("UNCOMMITTED-8k3\n")
        write(os.path.join(self.dir, "new.txt"), "".join(f"NEWFILE-6t1 line {i}\n" for i in range(5)))
        write(os.path.join(self.dir, "huge.txt"), "".join(f"HUGEFILE-2m8 line {i}\n" for i in range(300)))

        self.events = events_file([
            tooluse("t1", "Bash", "cd /work/repo && ./gradlew test"),
            toolres("t1", "Ran quiet\nBUILD SUCCESSFUL in 41s\n"),
            resultev("All done. The tests pass.", turns=7),
        ])

    def build(self, m, lookup=None):
        return reviewbundle.build_bundle(m, self.events, self.gitsh,
                                         lookup or (lambda i: None))

    def diff_part(self, bundle):
        return bundle.split("## Diff", 1)[1]

    def fences(self, text):
        return re.findall(r"```(?:text)?\n(.*?)\n```", text, re.S)


class TestDiffGitStart(RepoCase):
    def setUp(self):
        super().setUp()
        self.bundle = self.build(meta(git_start=self.start, git_branch_start="main"))

    def test_range_explained_in_header_and_diff(self):
        self.assertIn(f"- diff range: start commit {self.start[:9]}", self.bundle)
        self.assertIn(f"Range: start commit {self.start[:9]} recorded when the job began",
                      self.diff_part(self.bundle))

    def test_branch_from_meta_not_from_git(self):
        self.assertIn("- branch at start: main", self.bundle)   # git_branch_start recorded
        self.assertNotIn("- branch (now):", self.bundle)
        self.assertNotIn("\n- branch: ", self.bundle)           # the bare label is gone

    def test_committed_and_uncommitted_changes_present(self):
        self.assertIn("C1CHANGE-5r2", self.diff_part(self.bundle))
        self.assertIn("EARLYCOMMIT-3w9", self.diff_part(self.bundle))  # in the start..HEAD range
        self.assertIn("+UNCOMMITTED-8k3", self.diff_part(self.bundle))

    def test_no_context_lines(self):
        """-U0: the diff body carries no context lines (' ' at column 1)."""
        body = self.fences(self.diff_part(self.bundle))[1]
        self.assertFalse(re.search(r"^ [^ ].*$", body, re.M),
                         "-U0 expected, but a context line showed up:\n" + body)

    def test_filters_in_stat_not_in_body(self):
        stat, body = self.fences(self.diff_part(self.bundle))[0], \
                     self.fences(self.diff_part(self.bundle))[1]
        for name in ("package-lock.json", "big.py", "AGENT_REPORT.md"):
            self.assertIn(name, stat)
            self.assertNotIn(f"diff --git a/{name}", body)
        self.assertNotIn("LOCKMARKER-9x7", self.bundle)   # lockfile content never shown
        self.assertNotIn("GENERATED-9x7", self.bundle)    # 500-line file: no content
        self.assertIn("(big.py: 500 lines changed, over 400 lines, omitted - ask for it)",
                      self.diff_part(self.bundle))

    def test_report_shows_only_this_jobs_additions(self):
        """AGENT_REPORT.md existed before the job (modified in the diff), so only
        the added lines are shown - the older cumulative part is not repeated."""
        pre = self.bundle.split("## Diff")[0]
        self.assertIn("### AGENT_REPORT.md (this job's additions)", pre)
        self.assertIn("REPORTMARKER-4q2 this job added a section", pre)
        self.assertNotIn("STARTREP-0z1", self.bundle)     # older section not shown
        self.assertEqual(self.bundle.count("REPORTMARKER-4q2"), 1)
        self.assertNotIn("REPORTMARKER-4q2", self.diff_part(self.bundle))  # not in diff body either

    def test_pure_rename_is_one_line(self):
        self.assertIn("(renamed: c_move.txt -> c_moved.txt, no content changes)",
                      self.diff_part(self.bundle))
        body = self.fences(self.diff_part(self.bundle))[1]
        self.assertNotIn("-movable content", body)
        self.assertNotIn("+movable content", body)

    def test_untracked_files(self):
        part = self.diff_part(self.bundle)
        self.assertIn("NEWFILE-6t1 line 0", part)                       # small one: shown as + lines
        self.assertIn("(new untracked file: +5 lines)", part)
        self.assertIn("(new untracked file: huge.txt: 300 lines, too big to include)", part)
        self.assertNotIn("HUGEFILE-2m8", self.bundle)                   # big one: named only


class TestDiffTimeWindow(RepoCase):
    def setUp(self):
        super().setUp()
        self.bundle = self.build(meta())  # no git_start -> old-style time window

    def test_window_finds_exactly_the_in_window_commits(self):
        part = self.diff_part(self.bundle)
        self.assertIn(f"Range: 2 commits dated inside the job window, "
                      f"diff {self.early[:9]}..{self.c2[:9]}", part)
        self.assertIn("C1CHANGE-5r2", part)          # c1/c2 found
        self.assertNotIn("EARLYCOMMIT-3w9", self.bundle)  # backdated commits excluded
        self.assertNotIn("C3CHANGE-7d4", self.bundle)

    def test_single_commit_range_names_base_and_newest(self):
        b = self.build(meta(started=time.time() - 7200, finished=time.time() - 3500))
        part = self.diff_part(b)
        self.assertIn(f"1 commit dated inside the job window, diff {self.c2[:9]}..{self.c3[:9]}", part)
        self.assertIn("C3CHANGE-7d4", part)
        self.assertNotIn("C1CHANGE-5r2", part)

    def test_branch_shows_now_for_old_jobs(self):
        self.assertIn("- branch (now): ", self.bundle)

    def test_window_range_has_no_uncommitted_or_untracked(self):
        part = self.diff_part(self.bundle)
        self.assertNotIn("UNCOMMITTED-8k3", self.bundle)
        self.assertNotIn("NEWFILE-6t1", self.bundle)

    def test_filters_apply_too(self):
        stat, body = self.fences(self.diff_part(self.bundle))[0], \
                     self.fences(self.diff_part(self.bundle))[1]
        self.assertIn("package-lock.json", stat)
        self.assertNotIn("diff --git a/package-lock.json", body)
        self.assertIn("(big.py: 500 lines changed, over 400 lines, omitted - ask for it)",
                      self.diff_part(self.bundle))
        self.assertNotIn("GENERATED-9x7", self.bundle)


class TestDiffNoGit(unittest.TestCase):
    def test_non_git_folder_says_so(self):
        d = tempfile.mkdtemp(prefix="rb-nogit-")
        ev = events_file([resultev()])
        b = reviewbundle.build_bundle(meta(folder="d"), ev, gitof(d), lambda i: None)
        self.assertTrue(b.startswith("# Review bundle: j1"))
        self.assertIn("not a git repository", b)
        self.assertIn("no changes found", b.split("## Diff", 1)[1])


class TestUntrackedSafety(RepoCase):
    def test_shell_meta_in_a_filename_is_harmless(self):
        """File names come from an untrusted repo: they must never reach a shell
        command string, only a quoted "$f" loop variable inside one script."""
        write(os.path.join(self.dir, "a $(touch PWNED) b.txt"), "INJECT-3z5\n")
        b = self.build(meta(git_start=self.start, git_branch_start="main"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "PWNED")),
                         "a command embedded in a file name was executed")
        self.assertIn("INJECT-3z5", b)                      # the file itself is still shown

    def test_more_than_twenty_untracked_gets_one_note(self):
        for i in range(25):
            write(os.path.join(self.dir, f"u{i:02d}.txt"), f"untracked {i}\n")
        part = self.diff_part(self.build(meta(git_start=self.start, git_branch_start="main")))
        self.assertTrue(re.search(r"\(and \d+ more untracked files not shown\)", part), part)

    def test_at_most_six_git_calls_with_300_untracked(self):
        for i in range(300):
            write(os.path.join(self.dir, f"junk{i:03d}.txt"), f"junk {i}\n")
        calls = []

        def counting(script):
            calls.append(script)
            return self.gitsh(script)

        reviewbundle.build_bundle(meta(git_start=self.start, git_branch_start="main"),
                                  self.events, counting, lambda i: None)
        self.assertLessEqual(len(calls), 6, f"{len(calls)} git calls: {calls}")


class TestOmitOrder(RepoCase):
    def test_test_file_diffs_are_cut_before_source(self):
        m = meta(git_start=self.start, git_branch_start="main")
        full = reviewbundle.build_bundle(m, self.events, self.gitsh, lambda i: None,
                                         max_chars=10 ** 9)
        self.assertIn("TESTCODE-4h7 line 0", full)   # with room, everything is shown
        b = reviewbundle.build_bundle(m, self.events, self.gitsh, lambda i: None,
                                      max_chars=len(full) - 1500)
        self.assertIn("tests/test_a.py: 120 lines changed, omitted to fit the size limit - ask for it", b)
        self.assertNotIn("TESTCODE-4h7", b)
        self.assertIn("C1CHANGE-5r2", self.diff_part(b))  # the real source diff survives


class TestRefCopyFlood(unittest.TestCase):
    """A job that copies 50 reference files while changing 3 real ones - the
    exact way the real c25f bundle lost benchrun.py and friends. Two guards:
    the shrink order drops the piles of small copies FIRST and keeps the three
    bigger real files whole, and .reviewignore folds the copies into one stat
    line and out of the body entirely."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rb-refcopy-")

        def run(*args):
            subprocess.run(list(args), cwd=self.dir, check=True,
                           capture_output=True, text=True)

        self.gitsh = gitof(self.dir)
        run("git", "init", "-q")
        run("git", "config", "user.name", "Test")
        run("git", "config", "user.email", "test@example.com")
        write(os.path.join(self.dir, "old.py"), "OLD-0\n")
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "start")
        self.start = self.gitsh("git rev-parse HEAD")

        os.mkdir(os.path.join(self.dir, "ref-copy"))
        for i in range(50):
            write(os.path.join(self.dir, "ref-copy", f"ref{i:02d}.py"),
                  "".join(f"REFCOPY-{i:02d} reference filler {j} xxxxxxx\n" for j in range(20)))
        write(os.path.join(self.dir, "ref-copy", "not_added.py"), "REFCOPY-UNTRACKED line\n")
        for name in ("server", "client", "util"):
            write(os.path.join(self.dir, f"{name}.py"),
                  "".join(f"REALCODE-{name} core logic line {j} yyyyy\n" for j in range(40)))
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "reference copies plus the real work")
        self.events = events_file([resultev()])

    def build(self, max_chars=10 ** 9):
        return reviewbundle.build_bundle(meta(git_start=self.start, git_branch_start="main"),
                                         self.events, self.gitsh, lambda i: None,
                                         max_chars=max_chars)

    def diff_part(self, bundle):
        return bundle.split("## Diff", 1)[1]

    def fences(self, text):
        return re.findall(r"```(?:text)?\n(.*?)\n```", text, re.S)

    def test_size_cap_cuts_the_copies_not_the_three_real_files(self):
        full = self.build()
        self.assertIn("REFCOPY-07 reference filler 5", full)     # with room: everything
        self.assertIn("REALCODE-server core logic line 39", full)
        b = self.build(max_chars=len(full) - 12000)              # force omissions
        for name in ("server", "client", "util"):
            self.assertIn(f"REALCODE-{name} core logic line 39", b)   # all 3 whole
            self.assertNotIn(f"(REALCODE-{name}", b)
        self.assertNotIn("REFCOPY-07 reference filler 5", b)     # the copies got cut
        self.assertIn("omitted to fit the size limit - ask for it", b)

    def test_reviewignore_one_stat_line_and_absent_from_body(self):
        write(os.path.join(self.dir, ".reviewignore"),
              "# copies of reference code, never reviewed\nref-copy/\n")
        part = self.diff_part(self.build())
        stat, body = self.fences(part)[0], self.fences(part)[1]
        m = re.search(r"ref-copy/ \((\d+) files, \+([\d,]+) lines - in \.reviewignore, not shown\)", stat)
        self.assertIsNotNone(m, "no collapsed ignore line in:\n" + stat)
        self.assertEqual(int(m.group(1)), 51)         # 50 committed + 1 untracked
        self.assertEqual(int(m.group(2).replace(",", "")), 1001)   # 50*20 + 1 untracked
        self.assertNotIn("diff --git a/ref-copy/", body)
        self.assertNotIn("REFCOPY", self.build())     # even the untracked one is gone
        self.assertIn("REALCODE-server core logic line 39", self.build())

    def test_committed_reviewignore_works_too(self):
        write(os.path.join(self.dir, ".reviewignore"), "ref-copy/\n")
        subprocess.run(["git", "add", ".reviewignore"], cwd=self.dir, check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "commit", "-q", "-m", "add ignore"], cwd=self.dir, check=True,
                       capture_output=True, text=True)
        part = self.diff_part(self.build())
        self.assertIn("in .reviewignore, not shown", self.fences(part)[0])
        self.assertNotIn("REFCOPY-07", part)

    def test_reviewignore_catches_git_abbreviated_stat_names(self):
        """git --stat shortens long names with '...' in the middle ('...
        ...t_repo/tests/test_core.py'), which the raw pattern would not match.
        The numstat paths (never abbreviated) give the real name back."""
        # long enough (about 100 chars) that git --stat-width=100 must shorten
        # it with "..." no matter how wide the +/- graph is
        deep = os.path.join(self.dir, "ref-copy", "suite", "agent", "a1_textstats",
                            "reference_repo_extra", "nested", "deeper", "tests")
        os.makedirs(deep)
        write(os.path.join(deep, "test_core.py"),
              "".join(f"REFCOPY-DEEP reference line {i} zzz\n" for i in range(25)))
        write(os.path.join(self.dir, "widefile.py"),
              "".join(f"WIDEGRAPH line {i}\n" for i in range(700)))
        subprocess.run(["git", "add", "-A"], cwd=self.dir, check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "commit", "-q", "-m", "deep copy + wide file"],
                       cwd=self.dir, check=True, capture_output=True, text=True)
        raw = self.gitsh(f"git diff --stat --stat-width=100 {self.start} HEAD")
        if ".../" not in raw:
            self.skipTest("this git does not abbreviate stat names; nothing to test")
        plain = self.fences(self.diff_part(self.build()))[0]
        # even abbreviated, the line is counted in the ref-copy/ group, not
        # left standing on its own under "..." (git add -A above also committed
        # the fixture's untracked ref-copy/not_added.py: 50 + not_added + deep)
        self.assertEqual(re.search(r"ref-copy/ \((\d+) files", plain).group(1), "52")
        self.assertNotIn("test_core.py", plain)
        write(os.path.join(self.dir, ".reviewignore"), "ref-copy/\n")
        stat = self.fences(self.diff_part(self.build()))[0]
        self.assertNotIn("test_core.py", stat)
        self.assertEqual(re.search(r"ref-copy/ \((\d+) files", stat).group(1), "52")
        self.assertNotIn("REFCOPY-DEEP", self.build())
        self.assertIn("widefile.py", stat)             # not ignored: still listed

    def test_long_stat_folds_big_directories(self):
        os.mkdir(os.path.join(self.dir, "many"))
        for i in range(30):
            write(os.path.join(self.dir, "many", f"m{i:02d}.py"), f"MANY-{i} one line\n")
        os.mkdir(os.path.join(self.dir, "other"))
        for i in range(12):
            write(os.path.join(self.dir, "other", f"o{i:02d}.py"), f"OTHER-{i} one line\n")
        write(os.path.join(self.dir, "rootfix.py"), "ROOTFIX-1 one line\n")
        subprocess.run(["git", "add", "-A"], cwd=self.dir, check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "commit", "-q", "-m", "wide change"], cwd=self.dir, check=True,
                       capture_output=True, text=True)
        stat = self.fences(self.diff_part(self.build()))[0]
        self.assertIn("many/ (30 files, +30 -0)", stat)
        self.assertIn("other/ (12 files, +12 -0)", stat)
        self.assertNotIn("many/m07.py", stat)            # individual lines folded away
        self.assertNotIn("other/o11.py", stat)
        self.assertIn("rootfix.py", stat)                # small dirs and root stay
        self.assertIn("files changed", stat)             # git's summary line survives
        self.assertIn("MANY-7 one line", self.diff_part(self.build()))  # body unaffected


class TestIgnoreHelpers(unittest.TestCase):
    def test_parse_ignore(self):
        self.assertEqual(reviewbundle._parse_ignore("# c\n\n  bench-ref/  \n*.min.js\n#x"),
                         ["bench-ref/", "*.min.js"])

    def test_match_whole_path_and_prefixes(self):
        self.assertTrue(reviewbundle._ignore_match("bench-ref/hard/hard_ab.py", ["bench-ref/"]))
        self.assertTrue(reviewbundle._ignore_match("bench-ref/x.py", ["bench-ref"]))
        self.assertTrue(reviewbundle._ignore_match("a/b/app.min.js", ["*.min.js"]))
        self.assertTrue(reviewbundle._ignore_match("docs/sub/note.md", ["docs/*"]))
        self.assertFalse(reviewbundle._ignore_match("src/refs.py", ["ref/"]))
        self.assertFalse(reviewbundle._ignore_match("x.py", []))

    def test_low_value_files(self):
        for p in ("vendor/cJSON.c", "bench-ref/llm_bench.py", "reference/old.java",
                  "third_party/a/b.h", "node_modules/q/index.js", "static/app.min.js",
                  "bundle.js.map", "docs/guide.md", "data/feed.csv", "web/icon.svg"):
            self.assertTrue(reviewbundle._low_value(p), p)
        for p in ("jobboard.py", "AGENT_REPORT.md", "README.md", "PLAN.md",
                  "src/core.py", "plan.json"):            # project's own top-level files
            self.assertFalse(reviewbundle._low_value(p), p)

    def test_parse_numstat(self):
        ns = reviewbundle._parse_numstat(
            "10\t2\tref-copy/a.py\n-\t-\tbin.png\n3\t0\told.py => new.py\n"
            "4\t1\tbig/{old => new}.py\nnot a numstat line")
        self.assertEqual(ns["ref-copy/a.py"], (10, 2))
        self.assertEqual(ns["bin.png"], (0, 0))
        self.assertEqual(ns["new.py"], (3, 0))
        self.assertEqual(ns["big/new.py"], (4, 1))
        self.assertNotIn("not a numstat line", ns)


# --------------------------------------------------- build and test results (events)

BT_EVENTS = [
    tooluse("a", "Bash", "cd /work/x && python3 -m unittest discover -s tests"),
    toolres("a", "F test_fail\nTraceback (most recent call last):\n"
                 '  File "t.py", line 3\nAssertionError: nope\n'
                 "Ran 5 tests in 0.4s\n\nFAILED (failures=1)\n"),
    tooluse("b", "Bash", "cd /work/x && python3 -m unittest discover -s tests"),
    toolres("b", "Ran 5 tests in 0.4s\n\nOK\n"),
    tooluse("c", "Bash", "./gradlew test"),
    toolres("c", "".join("Downloading dep\n" for _ in range(20))
                 + "e: file.kt:12:5 Unresolved reference: foo\n"
                 + "FAILURE: Build failed with an exception.\nBUILD FAILED in 12s\n"),
    tooluse("d", "Bash", "./gradlew assembleDebug"),
    toolres("d", "".join("Downloading https://repo/x\n" for _ in range(40))
                 + "BUILD SUCCESSFUL in 41s\n"),
    tooluse("e", "Bash", "node --check app.js"),
    toolres("e", ""),
    resultev(),
]


class TestBuildTestResults(unittest.TestCase):
    def build(self, lines, tool=None):
        ev = lines
        if tool:  # same log, Qwen harness tool names
            ev = [json.loads(json.dumps(e)) for e in lines]
            for e in ev:
                for c in (e.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use":
                        c["name"] = tool
        p = events_file(ev)
        d = tempfile.mkdtemp(prefix="rb-nogit-")
        return reviewbundle.build_bundle(meta(), p, gitof(d), lambda i: None)

    def test_groups_last_run_only(self):
        b = self.build(BT_EVENTS)
        items = bt_items(b)
        self.assertIn(("PASS", "python3 -m unittest discover -s tests"), items)
        self.assertIn("(ran 2 times, failed 1 time before passing, last shown)",
                      items[("PASS", "python3 -m unittest discover -s tests")][0])
        self.assertNotIn("FAILED (failures=1)", b)  # the older failed run is gone
        self.assertNotIn("cd /work/x", b)           # cd prefixes normalised away

    def test_fail_block_shows_error_with_context(self):
        b = self.build(BT_EVENTS)
        items = bt_items(b)
        block = items[("FAIL", "./gradlew test")][1]
        self.assertIn("e: file.kt:12:5 Unresolved reference: foo", block)
        self.assertIn("BUILD FAILED in 12s", block)
        self.assertIn("FAILURE: Build failed with an exception.", block)
        self.assertNotIn("Downloading dep\nDownloading dep\nDownloading dep", block)  # noise trimmed

    def test_pass_block_summary_only(self):
        b = self.build(BT_EVENTS)
        items = bt_items(b)
        block = items[("PASS", "./gradlew assembleDebug")][1]
        self.assertEqual(block.strip(), "BUILD SUCCESSFUL in 41s")  # 40 download lines dropped

    def test_pass_without_summary_has_no_block(self):
        b = self.build(BT_EVENTS)
        self.assertNotIn("(no output)", b)
        i = b.index("- PASS `node --check app.js`")
        nxt = b[i:].splitlines()[1]
        self.assertFalse(nxt.startswith("```"), "PASS with nothing to show gets no fence")

    def test_exit_line_counts_as_summary_pass(self):
        cmd = "./gradlew -q test --offline 2>&1 | tail -5; echo \"EXIT=$?\""
        lines = [tooluse("g", "Bash", cmd), toolres("g", "EXIT=0\n"), resultev()]
        b = self.build(lines)
        items = bt_items(b)
        key = "./gradlew test --offline"  # -q, 2>&1, the tail stage and echo are gone
        self.assertEqual(list(items), [("PASS", key)])
        self.assertIn(f'(last: {cmd})', items[("PASS", key)][0])
        block = items[("PASS", key)][1]
        self.assertEqual(block.strip(), "EXIT=0")   # EXIT=0 alone is enough for PASS
        self.assertNotIn("no summary lines found", b)

    def test_exit_line_counts_as_summary_fail(self):
        lines = [tooluse("g", "Bash", "cargo test"), toolres("g", "   Compiling x\nexit status 2\n"), resultev()]
        b = self.build(lines)
        items = bt_items(b)
        self.assertIn(("FAIL", "cargo test"), items)
        self.assertIn("exit status 2", items[("FAIL", "cargo test")][1])

    def test_passing_run_that_prints_traceback_and_error(self):
        """Tests checking error handling print 'Traceback' / 'error:' on purpose;
        the trailing 'Ran N tests / OK' summary must decide, not the word scan."""
        out = ("test_bad_input ... ok\n"
               "Traceback (most recent call last):\n"
               '  File "t.py", line 9, in test_bad_input\nValueError: error: bad input handled\n'
               "Ran 4 tests in 0.1s\n\nOK\n")
        lines = [tooluse("t", "Bash", "python3 -m unittest discover -s tests"), toolres("t", out), resultev()]
        b = self.build(lines)
        items = bt_items(b)
        self.assertIn(("PASS", "python3 -m unittest discover -s tests"), items)
        block = items[("PASS", "python3 -m unittest discover -s tests")][1]
        self.assertEqual(block.strip(), "Ran 4 tests in 0.1s\nOK")
        self.assertNotIn("- FAIL", b)

    def test_is_error_always_fails(self):
        lines = [tooluse("t", "Bash", "./gradlew build"),
                 toolres("t", "BUILD SUCCESSFUL in 3s\n", err=True), resultev()]
        b = self.build(lines)
        self.assertIn(("FAIL", "./gradlew build"), bt_items(b))

    def test_fail_block_line_cap(self):
        noisy = "BEGIN\n" + "".join(f"error: thing {i} broke\n" for i in range(45)) + "END\n"
        lines = [tooluse("x", "Bash", "make test"), toolres("x", noisy), resultev()]
        b = self.build(lines)
        block = bt_items(b)[("FAIL", "make test")][1]
        n = len(block.strip().splitlines())
        self.assertLessEqual(n, 26)  # 25 error lines + the continuation note
        self.assertIn("error lines continue, 22 more not shown", block)

    def test_group_cap_eight(self):
        lines = []
        for i in range(1, 11):
            lines += [tooluse(f"p{i}", "Bash", f"pytest t{i}"), toolres(f"p{i}", "1 passed\n")]
        lines.append(resultev())
        b = self.build(lines)
        cmds = {cmd for _, cmd in bt_items(b)}
        self.assertEqual(len(cmds), 8)
        self.assertIn("pytest t10", cmds)
        self.assertNotIn("pytest t1", cmds)  # only the last 8 groups survive

    def test_qwen_harness(self):
        b = self.build(BT_EVENTS, tool="run_shell_command")
        items = bt_items(b)
        self.assertIn(("FAIL", "./gradlew test"), items)
        self.assertIn(("PASS", "python3 -m unittest discover -s tests"), items)

    def test_non_build_commands_ignored(self):
        lines = [tooluse("g", "Bash", "git status"), toolres("g", "nothing"),
                 tooluse("h", "Bash", "ls -la"), toolres("h", "files"), resultev()]
        b = self.build(lines)
        self.assertNotIn("## Build and test results", b)  # section omitted when empty

    def test_heredoc_file_write_not_a_test_run(self):
        """A python heredoc that merely mentions ./gradlew in the text it writes
        is the agent writing a plan file, not running a build."""
        cmd = "python3 -I - <<'EOF'\nopen('PLAN.md','w').write('run ./gradlew test next')\nEOF"
        lines = [tooluse("w", "Bash", cmd), toolres("w", "plan appended"), resultev()]
        b = self.build(lines)
        self.assertNotIn("## Build and test results", b)

    def test_long_command_display_capped(self):
        long_cmd = "pytest " + " ".join(f"--flag{i}=value{i}" for i in range(30))
        lines = [tooluse("q", "Bash", long_cmd), toolres("q", "3 passed\n"), resultev()]
        b = self.build(lines)
        line = [ln for ln in b.splitlines() if ln.startswith("- PASS")][0]
        self.assertLess(len(line), 150)
        self.assertTrue(line.endswith("...`"), line)

    def test_long_output_line_is_clipped(self):
        """A whole bundle string repr in one AssertionError line must not eat
        the bundle: every line in a build/test block is capped at 300 chars."""
        big = "AssertionError: " + "X" * 4900 + " ENDMARK"
        out = ("FAIL: test_bundle\n" + big + "\nRan 1 test in 0.2s\n\n"
               "FAILED (failures=1)\n")
        lines = [tooluse("z", "Bash", "python3 -m unittest discover -s tests"),
                 toolres("z", out), resultev()]
        b = self.build(lines)
        block = bt_items(b)[("FAIL", "python3 -m unittest discover -s tests")][1]
        clipped = [ln for ln in block.splitlines() if "chars cut" in ln]
        self.assertEqual(len(clipped), 1)
        self.assertTrue(clipped[0].startswith("AssertionError: XXXX"))
        self.assertTrue(clipped[0].endswith("ENDMARK"))
        self.assertLess(len(clipped[0]), 320)
        n = len(big) - 240 - 40
        self.assertIn(f" ... ({n} chars cut) ... ", clipped[0])
        self.assertIn("Ran 1 test in 0.2s", block)  # short lines untouched

    def test_long_summary_line_clipped_in_pass_block(self):
        out = "1 passed " + "y" * 900 + "\n"
        lines = [tooluse("z", "Bash", "pytest tests/x.py"), toolres("z", out), resultev()]
        b = self.build(lines)
        block = bt_items(b)[("PASS", "pytest tests/x.py")][1]
        clipped = [ln for ln in block.splitlines() if "chars cut" in ln]
        self.assertEqual(len(clipped), 1)
        self.assertLess(len(clipped[0]), 320)
        self.assertTrue(clipped[0].endswith("yyyy"))

    def test_same_suite_four_real_commands_one_group(self):
        """The four commands from Jason's own job bundle: py_compile && unittest,
        then | tail -25, then | tail -4, then | tail -3 && git commit. They are
        ONE group: key 'python3 -m unittest discover -s tests'."""
        cmds = ["python3 -m py_compile reviewbundle.py jobboard.py && "
                "python3 -m unittest discover -s tests -v",
                "python3 -m unittest discover -s tests -v 2>&1 | tail -25",
                "python3 -m unittest discover -s tests -v 2>&1 | tail -4",
                "python3 -m unittest discover -s tests -v 2>&1 | tail -3 "
                "&& git add -A && git commit -m ok"]
        outs = ["Ran 108 tests in 3.2s\nOK\n", "FAILED (failures=3)\n",
                "FAILED (failures=1)\n", "Ran 108 tests in 3.3s\nOK\n"]
        lines = []
        for i, (c, o) in enumerate(zip(cmds, outs)):
            lines += [tooluse(f"u{i}", "Bash", c), toolres(f"u{i}", o)]
        b = self.build(lines + [resultev()])
        items = bt_items(b)
        self.assertEqual(list(items), [("PASS", "python3 -m unittest discover -s tests")])
        note, block = items[("PASS", "python3 -m unittest discover -s tests")]
        self.assertIn("(ran 4 times, failed 2 times before passing, last shown)", note)
        self.assertIn(f"(last: {cmds[-1]})", note)
        self.assertEqual(block.strip(), "Ran 108 tests in 3.3s\nOK")
        self.assertNotIn("FAILED (failures", b)  # the older failed runs are not shown

    def test_gradle_flags_pipes_and_echo_one_group(self):
        c1 = ('./gradlew testDebugUnitTest assembleDebug --no-daemon -q 2>&1 '
              '| tail -30; echo "EXIT=$?"')
        lines = [tooluse("g1", "Bash", "./gradlew testDebugUnitTest assembleDebug"),
                 toolres("g1", "BUILD SUCCESSFUL in 39s\n"),
                 tooluse("g2", "Bash", c1),
                 toolres("g2", "BUILD SUCCESSFUL in 41s\nEXIT=0\n"), resultev()]
        b = self.build(lines)
        items = bt_items(b)
        self.assertEqual(list(items),
                         [("PASS", "./gradlew testDebugUnitTest assembleDebug")])
        note = items[("PASS", "./gradlew testDebugUnitTest assembleDebug")][0]
        self.assertIn("(ran 2 times, last shown)", note)
        self.assertIn(f"(last: {c1})", note)

    def test_pytest_two_files_stay_two_groups(self):
        lines = [tooluse("p1", "Bash", "pytest -x tests/a.py"),
                 toolres("p1", "1 passed\n"),
                 tooluse("p2", "Bash", "pytest tests/b.py"),
                 toolres("p2", "2 passed\n"), resultev()]
        b = self.build(lines)
        self.assertEqual({cmd for _, cmd in bt_items(b)},
                         {"pytest tests/a.py", "pytest tests/b.py"})

    def test_py_compile_alone_is_its_own_group(self):
        lines = [tooluse("c1", "Bash", "python3 -m py_compile jobboard.py"),
                 toolres("c1", ""),
                 tooluse("c2", "Bash", "python3 -m unittest discover -s tests"),
                 toolres("c2", "Ran 2 tests\nOK\n"), resultev()]
        b = self.build(lines)
        self.assertIn("- PASS `python3 -m py_compile jobboard.py`", b)
        self.assertIn(("PASS", "python3 -m unittest discover -s tests"), bt_items(b))


# -------------------------------------------------------------------- prompts

class TestPrompts(unittest.TestCase):
    def test_chain_prompts_truncated(self):
        parent = meta(id="p1", job="P" * 1000, parent=None)
        child = meta(id="c1", job="child prompt UNIQUE", parent="p1")
        ev = events_file([resultev()])
        d = tempfile.mkdtemp(prefix="rb-nogit-")
        b = reviewbundle.build_bundle(child, ev, gitof(d), lambda i: parent if i == "p1" else None)
        self.assertIn("### earlier job p1", b)
        self.assertIn("### this job c1", b)
        self.assertLess(b.index("### earlier job p1"), b.index("### this job c1"))  # oldest first
        self.assertIn("P" * 600, b)
        self.assertNotIn("P" * 601, b)
        self.assertIn("... (400 more chars)", b)
        self.assertIn("child prompt UNIQUE", b)

    def test_summary_cap(self):
        ev = events_file([resultev("S" * 9000)])
        d = tempfile.mkdtemp(prefix="rb-nogit-")
        b = reviewbundle.build_bundle(meta(), ev, gitof(d), lambda i: None)
        self.assertIn("S" * 4000, b)
        self.assertNotIn("S" * 4001, b)
        self.assertIn("(summary cut at 4000 chars)", b)


# ----------------------------------------------------------------- size cap

class TestSizeCap(RepoCase):
    def setUp(self):
        super().setUp()
        parent = meta(id="p1", job="P" * 5000, parent=None)
        self.m = meta(id="c1", job="the child job UNIQUE", parent="p1",
                      git_start=self.start, git_branch_start="main")
        # make the report big: fresh mtime (inside the window), over the 6000 cap
        write(os.path.join(self.dir, "AGENT_REPORT.md"), "REPORTMARKER-4q2\n" + "R" * 6500)
        self.lookup = lambda i: parent if i == "p1" else None

    def notes(self, b):
        # the size-cap notes are distinct from the "over 400 lines" note big.py gets
        return {"prompt": "(earlier prompt shortened to fit)" in b or "omitted to fit)" in b,
                "report": "AGENT_REPORT.md cut to fit" in b or "AGENT_REPORT.md omitted to fit" in b,
                "omit": "omitted to fit the size limit" in b}

    def test_shrink_order_and_survivors(self):
        full = reviewbundle.build_bundle(self.m, self.events, self.gitsh, self.lookup,
                                         max_chars=10 ** 9)
        n = self.notes(full)
        self.assertFalse(n["prompt"] or n["report"] or n["omit"])

        prev = None
        for budget in (len(full), len(full) - 800, len(full) - 2500,
                       len(full) - 6000, len(full) - 11500):
            b = reviewbundle.build_bundle(self.m, self.events, self.gitsh, self.lookup,
                                          max_chars=budget)
            n = self.notes(b)
            # tier order: an omitted file means the report was cut too, etc.
            if n["omit"]:
                self.assertTrue(n["report"], f"file omitted before report at budget {budget}")
            if n["report"]:
                self.assertTrue(n["prompt"], f"report cut before prompts at budget {budget}")
            # never cut: this job's prompt, the summary, the build/test results
            self.assertIn("the child job UNIQUE", b)
            self.assertIn("All done. The tests pass.", b)
            self.assertIn("BUILD SUCCESSFUL in 41s", b)
            prev = n
        # deep in the cap all three tiers fired
        self.assertTrue(all(prev.values()))

    def test_earlier_prompt_never_the_current_one(self):
        b = reviewbundle.build_bundle(self.m, self.events, self.gitsh, self.lookup, max_chars=400)
        self.assertIn("the child job UNIQUE", b)
        self.assertIn("## Job prompt", b)


# -------------------------------------------------------------------- footer

class TestFooter(unittest.TestCase):
    def test_counts_match(self):
        d = tempfile.mkdtemp(prefix="rb-nogit-")
        b = reviewbundle.build_bundle(meta(), events_file([resultev()]), gitof(d), lambda i: None)
        m = re.search(r"\(bundle: (\d+) chars, about (\d+) tokens\)\s*$", b)
        self.assertTrue(m, b[-120:])
        n, t = int(m.group(1)), int(m.group(2))
        self.assertEqual(n, len(b))
        self.assertEqual(t, round(n / 4))


if __name__ == "__main__":
    unittest.main()
