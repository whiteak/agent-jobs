import math, unittest
from solution import evaluate


class T(unittest.TestCase):
    def eq(self, e, v):
        r = evaluate(e)
        self.assertIsInstance(r, float, e)
        self.assertTrue(math.isclose(r, v, rel_tol=1e-9, abs_tol=1e-12), f"{e!r} -> {r}, want {v}")

    def test_basic(self):
        self.eq("1+2*3", 7); self.eq("(1+2)*3", 9); self.eq("10/4", 2.5); self.eq("7-2-1", 4)
        self.eq("8/2/2", 2); self.eq("  3 *\t( 2 + 1 ) ", 9); self.eq("12.75", 12.75); self.eq("0", 0)

    def test_power(self):
        self.eq("2^3^2", 512); self.eq("-2^2", -4); self.eq("(-2)^2", 4); self.eq("2^-1", 0.5)
        self.eq("2*3^2", 18); self.eq("-3^2*2", -18); self.eq("4^0.5", 2)

    def test_unary(self):
        self.eq("--3", 3); self.eq("-(-(2))", 2); self.eq("3--2", 5); self.eq("3*-2", -6); self.eq("-  -  -1", -1)

    def test_nested(self):
        self.eq("((((1))))+((2))", 3); self.eq("1-(2-(3-(4-5)))", 3); self.eq("2^(1+1)^(1+0)", 4)

    def test_zero_div(self):
        with self.assertRaises(ZeroDivisionError):
            evaluate("1/(2-2)")
        with self.assertRaises(ZeroDivisionError):
            evaluate("5/0")

    def test_errors(self):
        for bad in ["", "   ", "1+", "*2", "(1+2", "1+2)", "1.2.3", ".5", "5.", "2 3", "1+a", "+1",
                    "()", "1^", "3 $ 4", "(1)(2)", "1 . 5"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                evaluate(bad)


if __name__ == "__main__":
    unittest.main()
