import unittest
from solution import TTLCache


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class T(unittest.TestCase):
    def setUp(self):
        self.c = Clock(); self.k = TTLCache(3, 10, clock=self.c)

    def test_validation(self):
        for a in [(0, 1), (2, 0), (2, -1), (1.5, 1)]:
            with self.assertRaises(ValueError):
                TTLCache(*a, clock=self.c)
        with self.assertRaises(ValueError):
            self.k.put("a", 1, ttl=0)

    def test_lru(self):
        k = self.k
        k.put("a", 1); k.put("b", 2); k.put("c", 3)
        self.assertEqual(k.get("a"), 1)
        k.put("d", 4)
        self.assertNotIn("b", k)
        self.assertEqual(k.keys(), ["c", "a", "d"])
        self.assertEqual(len(k), 3)

    def test_contains_no_recency(self):
        k = self.k
        k.put("a", 1); k.put("b", 2); k.put("c", 3)
        self.assertIn("a", k)
        k.put("d", 4)
        self.assertNotIn("a", k)

    def test_expiry_boundary(self):
        k = self.k
        k.put("a", 1)
        self.c.t += 9.999
        self.assertEqual(k.get("a"), 1)
        self.c.t = 1010.0
        self.assertIsNone(k.get("a"))
        self.assertEqual(k.get("a", "x"), "x")
        self.assertEqual(len(k), 0)
        self.assertFalse(k.delete("a"))

    def test_expired_purged_before_lru(self):
        k = self.k
        k.put("a", 1); k.put("b", 2, ttl=1); k.put("c", 3)
        self.c.t += 2
        k.put("d", 4)
        self.assertEqual(k.keys(), ["a", "c", "d"])

    def test_replace_refreshes(self):
        k = self.k
        k.put("a", 1); k.put("b", 2); k.put("c", 3)
        self.c.t += 5
        k.put("a", 10)
        k.put("d", 4)
        self.assertEqual(k.keys(), ["c", "a", "d"])
        self.c.t += 6
        self.assertEqual(k.keys(), ["a", "d"])
        self.assertEqual(k.get("a"), 10)

    def test_get_does_not_extend(self):
        k = self.k
        k.put("a", 1)
        self.c.t += 8; k.get("a"); self.c.t += 3
        self.assertNotIn("a", k)

    def test_delete(self):
        k = self.k
        k.put("a", 1)
        self.assertTrue(k.delete("a")); self.assertFalse(k.delete("a")); self.assertEqual(len(k), 0)

    def test_replace_when_full_no_evict(self):
        k = self.k
        k.put("a", 1); k.put("b", 2); k.put("c", 3); k.put("b", 20)
        self.assertEqual(k.keys(), ["a", "c", "b"])

    def test_many(self):
        k = TTLCache(100, 50, clock=self.c)
        for i in range(10000):
            k.put(i, i); self.c.t += 0.01
        self.assertEqual(len(k), 100)
        self.assertEqual(k.keys(), list(range(9900, 10000)))


if __name__ == "__main__":
    unittest.main()
