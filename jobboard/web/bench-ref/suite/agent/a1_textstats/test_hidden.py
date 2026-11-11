"""Hidden grader for the textstats agent task. Run with cwd = the repo root (python -I test_hidden.py REPO)."""
import json, os, subprocess, sys, tempfile, unittest

REPO = os.path.abspath(sys.argv.pop(1)) if len(sys.argv) > 1 else os.getcwd()
sys.path.insert(0, REPO)
from textstats import core  # noqa: E402

TEXT = "The cat sat. The cat ran!\nthe dog -- 'sat' too; it's re-used\n"


def cli(*args, stdin=None):
    p = subprocess.run([sys.executable, "-m", "textstats", *args], cwd=REPO, input=stdin,
                       capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr


class Core(unittest.TestCase):
    def test_words(self):
        self.assertEqual(core.words("Hello, world -- it's 'quoted' re-use!"), ["Hello", "world", "it's", "quoted", "re-use"])
        self.assertEqual(core.words("--- '' -a- b'"), ["a", "b"])
        self.assertEqual(core.words(""), [])

    def test_counts_ignore_case(self):
        self.assertEqual(core.word_counts(TEXT)["The"], 2)
        self.assertEqual(core.word_counts(TEXT, ignore_case=True)["the"], 3)

    def test_top_ties(self):
        self.assertEqual(core.top_words("b a c b a d", 3), [("a", 2), ("b", 2), ("c", 1)])

    def test_bigrams(self):
        bg = core.bigrams("a b a b A b")
        self.assertEqual(bg[("a", "b")], 2); self.assertEqual(bg[("A", "b")], 1)
        self.assertEqual(core.bigrams("a b a b A b", ignore_case=True)[("a", "b")], 3)
        self.assertEqual(sum(core.bigrams("x").values()), 0)


class Cli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix=".txt"); os.close(fd)
        with open(cls.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(TEXT)

    def test_count_plain(self):
        rc, out, _ = cli("count", self.path, "--top", "3")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["The\t2", "cat\t2", "sat\t2"])

    def test_count_ignore_case_json(self):
        rc, out, _ = cli("count", self.path, "--top", "2", "--ignore-case", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), [{"word": "the", "count": 3}, {"word": "cat", "count": 2}])
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_bigrams(self):
        rc, out, _ = cli("bigrams", self.path, "--top", "2", "--ignore-case")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["the cat\t2", "cat ran\t1"])
        rc, out, _ = cli("bigrams", "-", "--json", stdin="a b a b")
        self.assertEqual(json.loads(out), [{"bigram": "a b", "count": 2}, {"bigram": "b a", "count": 1}])

    def test_stdin_and_default_top(self):
        rc, out, _ = cli("count", "-", stdin=" ".join(f"w{i:02d}" for i in range(15)))
        self.assertEqual(rc, 0); self.assertEqual(len(out.splitlines()), 10)
        self.assertEqual(out.splitlines()[0], "w00\t1")

    def test_stats(self):
        rc, out, _ = cli("stats", self.path)
        self.assertEqual(out.splitlines(), ["words: 12", "unique: 9", "lines: 2"])
        rc, out, _ = cli("stats", "-", stdin="a b\nb")
        self.assertEqual(out.splitlines(), ["words: 3", "unique: 2", "lines: 2"])
        rc, out, _ = cli("stats", "-", stdin="")
        self.assertEqual(out.splitlines(), ["words: 0", "unique: 0", "lines: 0"])

    def test_errors(self):
        rc, out, err = cli("count", self.path, "--top", "0")
        self.assertEqual(rc, 2); self.assertTrue(err.strip())
        rc, out, err = cli("count", os.path.join(REPO, "nope.txt"))
        self.assertEqual(rc, 1); self.assertIn("error: cannot read", err)


class Extras(unittest.TestCase):
    def test_readme_documents(self):
        txt = open(os.path.join(REPO, "README.md"), encoding="utf-8").read()
        for w in ["count", "bigrams", "stats", "--top", "--ignore-case", "--json"]:
            self.assertIn(w, txt)

    def test_repo_tests_pass_and_grew(self):
        p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=REPO,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        import re
        m = re.search(r"Ran (\d+) test", p.stderr)
        self.assertTrue(m and int(m.group(1)) >= 4, p.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
