import unittest
from solution import apply_patch, PatchError

BASE = "".join(f"line {i}\n" for i in range(1, 21))


class T(unittest.TestCase):
    def test_simple(self):
        d = "--- a/f\n+++ b/f\n@@ -2,3 +2,3 @@\n line 2\n-line 3\n+LINE THREE\n line 4\n"
        self.assertEqual(apply_patch(BASE, d), BASE.replace("line 3\n", "LINE THREE\n"))

    def test_two_hunks_and_trailing_text(self):
        d = ("@@ -1,2 +1,3 @@ def foo():\n line 1\n+inserted\n line 2\n"
             "@@ -19,2 +20,1 @@\n line 19\n-line 20\n")
        want = BASE.replace("line 1\n", "line 1\ninserted\n").replace("line 20\n", "")
        self.assertEqual(apply_patch(BASE, d), want)

    def test_offset(self):
        text = "header\nheader2\n" + BASE  # everything shifted by +2
        d = "@@ -5,3 +5,2 @@\n line 5\n-line 6\n line 7\n"
        self.assertEqual(apply_patch(text, d), text.replace("line 6\n", ""))

    def test_offset_prefers_nearest_then_earlier(self):
        text = "x\nA\nx\nB\nx\nA\nx\n"  # 'x' at indexes 0,2,4,6
        d = "@@ -4,1 +4,1 @@\n-x\n+Y\n"  # expected index 3 ('B'); candidates 2 and 4 at distance 1 -> 2 wins
        self.assertEqual(apply_patch(text, d), "x\nA\nY\nB\nx\nA\nx\n")
        d2 = "@@ -1,1 +1,1 @@\n-B\n+Q\n"
        self.assertEqual(apply_patch(text, d2), "x\nA\nx\nQ\nx\nA\nx\n")

    def test_offsets_do_not_carry(self):
        text = "pad\n" + "a\nb\nc\n" + "a\nb\nc\n"
        # first hunk expects 'b' at index 1 (it is at 2 and 5) -> 2; second hunk expects 'c' at index 5 -> 6
        d = "@@ -2,1 +2,1 @@\n-b\n+B\n@@ -6,1 +6,1 @@\n-c\n+C\n"
        self.assertEqual(apply_patch(text, d), "pad\na\nB\nc\na\nb\nC\n")

    def test_no_overlap_with_previous(self):
        text = "a\nb\na\nb\n"
        d = "@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -1,1 +1,1 @@\n-a\n+A\n"
        self.assertEqual(apply_patch(text, d), "a\nB\nA\nb\n")

    def test_no_newline_add_and_remove(self):
        text = "one\ntwo"
        d = "@@ -1,2 +1,2 @@\n one\n-two\n\\ No newline at end of file\n+two\n"
        self.assertEqual(apply_patch(text, d), "one\ntwo\n")
        d3 = "@@ -2,1 +2,2 @@\n-two\n\\ No newline at end of file\n+two\n+three\n\\ No newline at end of file\n"
        self.assertEqual(apply_patch(text, d3), "one\ntwo\nthree")

    def test_newline_must_match(self):
        with self.assertRaises(PatchError):
            apply_patch("one\ntwo", "@@ -2,1 +2,1 @@\n-two\n+TWO\n")

    def test_insertion_only(self):
        self.assertEqual(apply_patch("a\nb\n", "@@ -0,0 +1,1 @@\n+start\n"), "start\na\nb\n")
        self.assertEqual(apply_patch("a\nb\n", "@@ -2,0 +3,2 @@\n+c\n+d\n"), "a\nb\nc\nd\n")
        self.assertEqual(apply_patch("", "@@ -0,0 +1,2 @@\n+x\n+y\n"), "x\ny\n")
        with self.assertRaises(PatchError):
            apply_patch("a\n", "@@ -5,0 +5,1 @@\n+z\n")

    def test_omitted_counts_and_blank_context(self):
        text = "a\n\nb\n"
        d = "@@ -1,3 +1,3 @@\n a\n\n-b\n+B\n"
        self.assertEqual(apply_patch(text, d), "a\n\nB\n")
        self.assertEqual(apply_patch("a\nb\n", "@@ -2 +2 @@\n-b\n+c\n"), "a\nc\n")

    def test_errors(self):
        with self.assertRaises(PatchError):
            apply_patch(BASE, "@@ -2,3 +2,3 @@\n line 2\n-line X\n+y\n line 4\n")
        with self.assertRaises(PatchError):
            apply_patch(BASE, "@@ -2,2 +2,2 @@\n line 2\n-line 3\n+y\n line 4\n")


if __name__ == "__main__":
    unittest.main()
