import unittest
from solution import find_slots


class T(unittest.TestCase):
    def test_basic(self):
        cals = [[("09:00", "10:30"), ("12:00", "13:00")], [("11:00", "11:30"), ("09:30", "10:00")]]
        wh = [("09:00", "17:00"), ("08:00", "13:30")]
        self.assertEqual(find_slots(cals, wh, 30, step=30),
                         [("10:30", "11:00"), ("11:30", "12:00"), ("13:00", "13:30")])

    def test_overlapping_candidates_and_limit(self):
        r = find_slots([[]], [("09:00", "11:00")], 60, step=15)
        self.assertEqual(r, [("09:00", "10:00"), ("09:15", "10:15"), ("09:30", "10:30"), ("09:45", "10:45"), ("10:00", "11:00")])
        self.assertEqual(find_slots([[]], [("09:00", "11:00")], 60, step=15, max_results=2), r[:2])

    def test_step_alignment(self):
        r = find_slots([[("09:00", "09:10")]], [("09:05", "10:00")], 20, step=15)
        self.assertEqual(r, [("09:15", "09:35"), ("09:30", "09:50")])

    def test_unsorted_overlapping_busy(self):
        cals = [[("14:00", "15:00"), ("09:00", "12:00"), ("11:00", "14:30")]]
        self.assertEqual(find_slots(cals, [("08:00", "16:00")], 60, step=60),
                         [("08:00", "09:00"), ("15:00", "16:00")])

    def test_end_of_day(self):
        self.assertEqual(find_slots([[("00:00", "23:00")]], [("00:00", "24:00")], 60, step=60), [("23:00", "24:00")])

    def test_none(self):
        self.assertEqual(find_slots([[], []], [("09:00", "10:00"), ("10:00", "11:00")], 15), [])

    def test_errors(self):
        bad_calls = [
            lambda: find_slots([[("10:00", "09:00")]], [("09:00", "17:00")], 30),
            lambda: find_slots([[]], [("09:00", "17:00")], 0),
            lambda: find_slots([[], []], [("09:00", "17:00")], 30),
            lambda: find_slots([[("9:00", "10:00")]], [("09:00", "17:00")], 30),
            lambda: find_slots([[("09:00", "10:60")]], [("09:00", "17:00")], 30),
            lambda: find_slots([[("24:00", "24:00")]], [("09:00", "17:00")], 30),
            lambda: find_slots([[]], [("09:00", "17:00")], 30, step=0),
        ]
        for i, f in enumerate(bad_calls):
            with self.assertRaises(ValueError, msg=str(i)):
                f()


if __name__ == "__main__":
    unittest.main()
