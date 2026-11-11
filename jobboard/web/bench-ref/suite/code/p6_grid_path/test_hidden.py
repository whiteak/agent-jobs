import random, time, unittest
from solution import cheapest_route
from reference_check import best_cost


def cost_of(c):
    return int(c) if c.isdigit() else 1


def validate(tc, grid, max_keys, result, want_cost):
    tc.assertIsNotNone(result)
    cost, path = result
    tc.assertEqual(cost, want_cost)
    path = [tuple(p) for p in path]
    tc.assertEqual(grid[path[0][0]][path[0][1]], "S")
    tc.assertEqual(grid[path[-1][0]][path[-1][1]], "E")
    keys = set(); total = 0
    for (r0, c0), (r1, c1) in zip(path, path[1:]):
        tc.assertEqual(abs(r0 - r1) + abs(c0 - c1), 1, "not adjacent")
        ch = grid[r1][c1]
        tc.assertNotEqual(ch, "#")
        if ch in "ABCD":
            tc.assertIn(ch.lower(), keys, f"door {ch} without key")
        if ch in "abcd" and ch not in keys and len(keys) < max_keys:
            keys.add(ch)
        total += cost_of(ch)
    tc.assertEqual(total, cost, "path cost does not add up")


class T(unittest.TestCase):
    def test_simple(self):
        g = ["S.9",
             "1#.",
             "11E"]
        validate(self, g, 0, cheapest_route(g), 4)

    def test_unreachable(self):
        self.assertIsNone(cheapest_route(["S#E"]))
        self.assertIsNone(cheapest_route(["SAE", "#a#"]))

    def test_door(self):
        g = ["S.A.E",
             "#.###",
             "#a###"]
        validate(self, g, 1, cheapest_route(g, max_keys=1), 8)

    def test_key_limit(self):
        g = ["SbaAE",
             "#9999"]
        validate(self, g, 1, cheapest_route(g, max_keys=1), 30)
        validate(self, g, 2, cheapest_route(g, max_keys=2), 4)

    def test_revisit_after_key(self):
        g = ["9S1A1E",
             "a#####"]
        validate(self, g, 1, cheapest_route(g, max_keys=1), best_cost(g, 1))

    def test_errors(self):
        for g in [["S.."], ["SSE"], ["S.E", ".."], ["S?E"], ["SEE"], [], ["S.Eg"]]:
            with self.assertRaises(ValueError, msg=repr(g)):
                cheapest_route(g)

    def test_big(self):
        rnd = random.Random(7)
        n = 200
        rows = [[rnd.choice("123456789...") for _ in range(n)] for _ in range(n)]
        for _ in range(4000):
            rows[rnd.randrange(n)][rnd.randrange(n)] = "#"
        for k in "abcd":
            for _ in range(3):
                rows[rnd.randrange(n)][rnd.randrange(n)] = k
            for _ in range(40):
                rows[rnd.randrange(n)][rnd.randrange(n)] = k.upper()
        rows[0][0] = "S"; rows[n - 1][n - 1] = "E"
        g = ["".join(r) for r in rows]
        t0 = time.perf_counter(); r = cheapest_route(g, max_keys=2); dt = time.perf_counter() - t0
        self.assertLess(dt, 5.0)
        validate(self, g, 2, r, best_cost(g, 2))


if __name__ == "__main__":
    unittest.main()
