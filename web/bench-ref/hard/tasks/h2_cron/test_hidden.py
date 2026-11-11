import time, unittest
from datetime import datetime as D
from solution import next_fire


class T(unittest.TestCase):
    def eq(self, e, after, want):
        self.assertEqual(next_fire(e, after), want, f"{e!r} after {after}")

    def test_basic(self):
        self.eq("* * * * *", D(2026, 11, 8, 10, 0, 30), D(2026, 11, 8, 10, 1))
        self.eq("*/15 * * * *", D(2026, 11, 8, 10, 7), D(2026, 11, 8, 10, 15))
        self.eq("*/15 * * * *", D(2026, 11, 8, 10, 15), D(2026, 11, 8, 10, 30))
        self.eq("59 23 31 12 *", D(2026, 12, 31, 23, 59), D(2027, 12, 31, 23, 59))
        self.eq("0 0 1 * *", D(2027, 1, 31, 23, 59, 59), D(2027, 2, 1))

    def test_steps_and_ranges(self):
        self.eq("5/20 10-22/6 * * *", D(2026, 11, 8, 22, 45), D(2026, 11, 9, 10, 5))
        self.eq("5/20 10-22/6 * * *", D(2026, 11, 8, 16, 25), D(2026, 11, 8, 16, 45))
        self.eq("0,30 9-10 * * *", D(2026, 11, 8, 10, 30), D(2026, 11, 9, 9, 0))

    def test_names(self):
        self.eq("0 9 * * MON-FRI", D(2026, 11, 7, 12, 0), D(2026, 11, 9, 9, 0))
        self.eq("0 12 * jan,Jul sun", D(2026, 11, 8), D(2027, 1, 3, 12, 0))
        self.eq("0 0 * * 7", D(2026, 11, 9), D(2026, 11, 15))
        self.eq("  0 0 * * 0  ", D(2026, 11, 9), D(2026, 11, 15))

    def test_day_or_rule(self):
        self.eq("0 0 13 * MON", D(2026, 11, 8), D(2026, 11, 9))
        self.eq("0 0 13 * MON", D(2026, 11, 9), D(2026, 11, 13))
        self.eq("30 4 1,15 * 5", D(2026, 11, 8), D(2026, 11, 13, 4, 30))

    def test_day_star_rule(self):
        self.eq("0 0 */2 * MON", D(2026, 11, 8), D(2026, 11, 9))
        self.eq("0 0 */2 * MON", D(2026, 11, 9), D(2026, 11, 23))

    def test_macros_and_leap(self):
        self.eq("@weekly", D(2026, 11, 8), D(2026, 11, 15))
        self.eq("@hourly", D(2026, 11, 8, 23, 0), D(2026, 11, 9, 0, 0))
        self.eq("@yearly", D(2026, 11, 8), D(2027, 1, 1))
        self.eq("0 0 29 2 *", D(2026, 3, 1), D(2028, 2, 29))

    def test_errors(self):
        for e in ["* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8", "5-1 * * * *",
                  "*/0 * * * *", "* * * FOO *", "1,,2 * * * *", "0 0 31 4 *", "0 0 30 2 *", "a * * * *"]:
            with self.assertRaises(ValueError, msg=e):
                next_fire(e, D(2026, 1, 1))

    def test_fast(self):
        t = time.time()
        for e in ["0 0 29 2 *", "0 0 31 4 *", "59 23 31 12 *"]:
            try:
                next_fire(e, D(2026, 3, 1))
            except ValueError:
                pass
        self.assertLess(time.time() - t, 3.0)


if __name__ == "__main__":
    unittest.main()
