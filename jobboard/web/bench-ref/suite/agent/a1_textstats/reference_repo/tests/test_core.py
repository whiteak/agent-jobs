import unittest
from textstats import core


class CoreTest(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(core.word_counts("a b a")["a"], 2)


if __name__ == "__main__":
    unittest.main()


class More(unittest.TestCase):
    def test_words(self):
        self.assertEqual(core.words("it's re-use!"), ["it's", "re-use"])

    def test_bigrams(self):
        self.assertEqual(core.bigrams("a b a b")[("a", "b")], 2)

    def test_ignore_case(self):
        self.assertEqual(core.word_counts("A a", ignore_case=True)["a"], 2)
