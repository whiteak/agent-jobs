import time, unittest
from solution import plan


def check_cycle(tc, tasks, cyc):
    tc.assertTrue(cyc, "empty cycle")
    tc.assertEqual(len(set(cyc)), len(cyc))
    for i, t in enumerate(cyc):
        nxt = cyc[(i + 1) % len(cyc)]
        tc.assertIn(nxt, tasks.get(t, []), f"{t} does not depend on {nxt}")


class T(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(plan({"b": ["a"], "a": [], "c": ["a"]}), ("order", ["a", "b", "c"]))

    def test_lex_smallest(self):
        tasks = {"d": [], "c": ["d"], "b": [], "a": ["c"]}
        self.assertEqual(plan(tasks), ("order", ["b", "d", "c", "a"]))

    def test_external_and_dupes(self):
        tasks = {"build": ["fetch", "fetch", "configure"], "configure": ["fetch"], "test": ["build"]}
        self.assertEqual(plan(tasks), ("order", ["fetch", "configure", "build", "test"]))

    def test_empty(self):
        self.assertEqual(plan({}), ("order", []))

    def test_self_loop(self):
        k, c = plan({"a": ["a"], "b": []})
        self.assertEqual((k, c), ("cycle", ["a"]))

    def test_cycle(self):
        tasks = {"a": ["b"], "b": ["c"], "c": ["d", "e"], "d": ["b"], "e": [], "x": ["a"]}
        k, c = plan(tasks)
        self.assertEqual(k, "cycle")
        check_cycle(self, tasks, c)
        self.assertEqual(set(c), {"b", "c", "d"})

    def test_big_chain(self):
        n = 50000
        tasks = {f"t{i:06d}": [f"t{i-1:06d}"] if i else [] for i in range(n)}
        t0 = time.perf_counter(); k, o = plan(tasks); dt = time.perf_counter() - t0
        self.assertEqual(k, "order"); self.assertEqual(o, sorted(tasks)); self.assertLess(dt, 2.0)

    def test_big_cycle(self):
        n = 50000
        tasks = {f"t{i}": [f"t{(i+1) % n}"] for i in range(n)}
        tasks["root"] = ["t0"]
        t0 = time.perf_counter(); k, c = plan(tasks); dt = time.perf_counter() - t0
        self.assertEqual(k, "cycle"); check_cycle(self, tasks, c); self.assertEqual(len(c), n); self.assertLess(dt, 2.0)


if __name__ == "__main__":
    unittest.main()
