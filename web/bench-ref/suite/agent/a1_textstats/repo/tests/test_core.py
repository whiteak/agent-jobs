import unittest
from textstats import core


class CoreTest(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(core.word_counts("a b a")["a"], 2)


if __name__ == "__main__":
    unittest.main()
