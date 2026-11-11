import time, unittest
from solution import match


class T(unittest.TestCase):
    def yes(self, p, s): self.assertTrue(match(p, s), f"{p!r} should match {s!r}")
    def no(self, p, s): self.assertFalse(match(p, s), f"{p!r} should not match {s!r}")

    def test_literals(self):
        self.yes("abc", "abc"); self.no("abc", "abcd"); self.no("abc", "ab"); self.yes("", ""); self.no("", "a")

    def test_dot_and_quant(self):
        self.yes("a.c", "abc"); self.yes("a.*c", "ac"); self.yes("a.*c", "axxxc"); self.no("a.+c", "ac")
        self.yes("colou?r", "color"); self.yes("colou?r", "colour"); self.no("colou?r", "colouur")
        self.yes("a*b*", ""); self.yes("x+y+", "xxyy"); self.no("x+y+", "xx")

    def test_escape(self):
        self.yes(r"a\.b", "a.b"); self.no(r"a\.b", "axb"); self.yes(r"\\", "\\"); self.yes(r"\*+", "***")
        self.yes(r"\(x\)", "(x)")

    def test_classes(self):
        self.yes("[a-c]+", "abccba"); self.no("[a-c]+", "abd"); self.yes("[^0-9]*", "abc"); self.no("[^0-9]*", "a1")
        self.yes("[]a]+", "]a]"); self.yes("[-a]+", "-a-"); self.yes("[a-]+", "a-"); self.yes(r"[\]x]+", "]x")
        self.yes("[A-Za-z_][A-Za-z0-9_]*", "_var9"); self.no("[A-Za-z_][A-Za-z0-9_]*", "9var")
        self.yes("[.]", "."); self.no("[.]", "a")

    def test_groups_alt(self):
        self.yes("(ab)+", "ababab"); self.no("(ab)+", "aba"); self.yes("cat|dog", "dog"); self.no("cat|dog", "catdog")
        self.yes("a(|b)c", "ac"); self.yes("a(|b)c", "abc"); self.yes("(a|b)*abb", "babaabb")
        self.yes("((a|b)(c|d))?e", "e"); self.yes("((a|b)(c|d))?e", "bde"); self.no("((a|b)(c|d))?e", "abe")
        self.yes("x(y(z|w)+)*", "xyzwyz"); self.yes("|a", ""); self.yes("|a", "a")

    def test_errors(self):
        for bad in ["(ab", "ab)", "*a", "a**", "[abc", "abc\\", "a|*", "(+)", "?"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                match(bad, "x")

    def test_no_blowup(self):
        t0 = time.perf_counter()
        self.no("(a*)*b", "a" * 30); self.no("(a|aa)*c", "a" * 40); self.no("(a+)+b", "a" * 30)
        self.yes("(a|aa)*", "a" * 40)
        self.assertLess(time.perf_counter() - t0, 1.0)


if __name__ == "__main__":
    unittest.main()
