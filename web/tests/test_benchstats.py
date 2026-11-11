#!/usr/bin/env python3
"""Tests for benchstats: hand-checked means/spreads, the A/B verdicts, the
"not enough samples" rule, determinism, env_diff and spec_diff, the rule that a
failed request is dropped (not graded 0), and the MEASURED false-positive and
detection rates of the verdict (CalibrationTests)."""
import random
import statistics
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import benchstats  # noqa: E402


def s(task, sample, passed, total, tps=20.0, secs=10.0):
    return {"task": task, "sample": sample, "passed": passed, "total": total,
            "score": passed / total, "decode_tps": tps, "total_s": secs}


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        self.samples = [
            s("t1", 1, 8, 8), s("t1", 2, 4, 8), s("t1", 3, 0, 8),
            s("t2", 1, 6, 6), s("t2", 2, 3, 6),
        ]
        self.sum = benchstats.summarize(self.samples)

    def test_per_task_hand_checked(self):
        t1 = self.sum["t1"]
        self.assertEqual(t1["n"], 3)
        self.assertEqual(t1["mean"], 50.0)                     # (100+50+0)/3
        self.assertAlmostEqual(t1["sd"], statistics.stdev([100, 50, 0]), 6)  # 50.0
        self.assertEqual(t1["min"], 0.0)
        self.assertEqual(t1["max"], 100.0)
        self.assertEqual(t1["scores"], [100.0, 50.0, 0.0])
        t2 = self.sum["t2"]
        self.assertAlmostEqual(t2["mean"], 75.0)
        self.assertAlmostEqual(t2["sd"], statistics.stdev([100, 50]), 6)

    def test_overall_is_mean_of_task_means(self):
        o = self.sum["overall"]
        self.assertEqual(o["n"], 5)
        self.assertEqual(o["mean"], 62.5)                      # (50 + 75) / 2, equal weights
        self.assertEqual(o["scores"], [50.0, 75.0])            # per-task means
        self.assertGreater(o["sd"], 0)                         # bootstrap spread exists
        self.assertEqual(o["min"], 0.0)
        self.assertEqual(o["max"], 100.0)

    def test_speed_metrics(self):
        samples = [s("t1", 1, 1, 1, tps=10.0, secs=4.0), s("t1", 2, 1, 1, tps=30.0, secs=6.0)]
        out = benchstats.summarize(samples)
        self.assertAlmostEqual(out["t1"]["decode_tps_mean"], 20.0)
        self.assertAlmostEqual(out["t1"]["secs_mean"], 5.0)
        self.assertAlmostEqual(out["overall"]["decode_tps_mean"], 20.0)

    def test_missing_speed_metric_is_none_not_zero(self):
        out = benchstats.summarize([{"task": "t1", "sample": 1, "passed": 1, "total": 2}])
        self.assertIsNone(out["t1"]["decode_tps_mean"])
        self.assertEqual(out["t1"]["mean"], 50.0)

    def test_single_sample_sd_zero_and_sd_deterministic(self):
        out = benchstats.summarize([s("t1", 1, 1, 2)])
        self.assertEqual(out["t1"]["sd"], 0.0)
        again = benchstats.summarize(self.samples)
        self.assertEqual(again["overall"]["sd"], self.sum["overall"]["sd"])

    def test_zero_total_scores_zero(self):
        out = benchstats.summarize([{"task": "t1", "sample": 1, "passed": 0, "total": 0}])
        self.assertEqual(out["t1"]["mean"], 0.0)


class CompareTests(unittest.TestCase):
    def cmp(self, a, b, **kw):
        return benchstats.compare(a, b, n_boot=500, seed=3, **kw)

    def two_task_run(self, score_fn):
        out = []
        for t, tot in (("t1", 8), ("t2", 6)):
            for i in range(5):
                out.append(s(t, i + 1, score_fn(t, i), tot))
        return out

    def test_clearly_better(self):
        A = self.two_task_run(lambda t, i: 1 if t == "t1" else 0)      # t1 tiny, t2 zero
        B = self.two_task_run(lambda t, i: 8 if t == "t1" else 6)      # everything full
        r = self.cmp(A, B)
        self.assertEqual(r["common"], ["t1", "t2"])
        self.assertEqual(r["overall"]["verdict"], "better")
        self.assertGreater(r["overall"]["lo"], 0)
        self.assertAlmostEqual(r["overall"]["mean_a"], 6.25)   # (12.5 + 0)/2
        self.assertAlmostEqual(r["overall"]["mean_b"], 100.0)
        self.assertAlmostEqual(r["overall"]["diff"], 93.75)    # B - A
        for row in r["tasks"]:
            self.assertEqual(row["verdict"], "better")
            self.assertGreater(row["lo"], 0)
            self.assertEqual(row["n_a"], 5)

    def test_clearly_worse(self):
        A = self.two_task_run(lambda t, i: 8 if t == "t1" else 6)
        B = self.two_task_run(lambda t, i: 1 if t == "t1" else 0)
        r = self.cmp(A, B)
        self.assertEqual(r["overall"]["verdict"], "worse")
        self.assertLess(r["overall"]["hi"], 0)

    def test_identical_runs_no_real_difference(self):
        A = self.two_task_run(lambda t, i: (4 if i % 2 else 8) if t == "t1" else 3)
        r = self.cmp(A, list(A))
        self.assertEqual(r["overall"]["verdict"], "no real difference")
        self.assertAlmostEqual(r["overall"]["diff"], 0.0)
        self.assertLessEqual(r["overall"]["lo"], 0)
        self.assertGreaterEqual(r["overall"]["hi"], 0)

    def test_not_enough_samples_rule(self):
        A = self.two_task_run(lambda t, i: 4)
        B = self.two_task_run(lambda t, i: 8 if t == "t1" else 6)
        few = [s("t1", 1, 8, 8)] + [s("t2", i, 6, 6) for i in range(1, 6)]
        r = self.cmp(A, few)
        self.assertEqual(r["overall"]["verdict"], "not enough samples")
        self.assertEqual(r["overall"]["n_b"], 6)               # numbers still shown
        self.assertEqual(r["tasks"][0]["n_b"], 1)

    def test_only_common_tasks_compared(self):
        A = [s("t1", i, 4, 8) for i in range(1, 6)] + [s("only_a", 1, 1, 1)]
        B = [s("t1", i, 8, 8) for i in range(1, 6)]
        r = self.cmp(A, B)
        self.assertEqual(r["common"], ["t1"])
        self.assertEqual(len(r["tasks"]), 1)

    def test_same_seed_is_deterministic(self):
        A = self.two_task_run(lambda t, i: 3 + (i % 3))
        B = self.two_task_run(lambda t, i: 5 + (i % 4))
        self.assertEqual(self.cmp(A, B), self.cmp(A, B))

    def test_speed_rows(self):
        A = [s("t1", i, 4, 8, tps=10.0, secs=20.0) for i in range(1, 6)]
        B = [s("t1", i, 4, 8, tps=40.0, secs=5.0) for i in range(1, 6)]
        r = self.cmp(A, B)
        d = r["speed"]["decode_tps"]
        self.assertEqual(d["verdict"], "faster")
        self.assertAlmostEqual(d["pct"], 300.0)
        sec = r["speed"]["secs"]
        self.assertEqual(sec["verdict"], "faster")             # fewer secs per answer
        self.assertAlmostEqual(sec["pct"], -75.0)
        self.assertEqual(r["overall"]["verdict"], "no real difference")  # scores identical

    def test_no_common_tasks(self):
        r = self.cmp([s("t1", 1, 1, 1)], [s("t2", 1, 1, 1)])
        self.assertEqual(r["common"], [])
        self.assertIsNone(r["overall"])


class BadLinesTests(unittest.TestCase):
    """A sample whose REQUEST failed is not a score, and must never be graded 0."""

    def cmp(self, a, b, **kw):
        return benchstats.compare(a, b, n_boot=500, n_perm=500, seed=3, **kw)

    def test_error_lines_are_dropped_and_counted(self):
        samples = [s("t1", 1, 8, 8), s("t1", 2, 4, 8),
                   {"task": "t1", "sample": 3, "passed": 0, "total": 8, "error": "HTTPError: 503"},
                   {"task": "t2", "sample": 1, "passed": 0, "total": 0, "error": "URLError"}]
        out = benchstats.summarize(samples)
        self.assertEqual(out["t1"]["n"], 2)                    # the failed pair is not data
        self.assertEqual(out["t1"]["mean"], 75.0)              # not dragged down to 0
        self.assertNotIn("t2", out)
        self.assertEqual(out["overall"]["n"], 2)
        self.assertEqual(out["overall"]["ignored"], 2)         # reported, not hidden

    def test_cut_off_answers_are_scores_and_are_counted(self):
        samples = [s("t1", 1, 8, 8), dict(s("t1", 2, 0, 8), finish="length")]
        out = benchstats.summarize(samples)
        self.assertEqual(out["t1"]["n"], 2)                    # kept: a cut answer IS a score
        self.assertEqual(out["t1"]["mean"], 50.0)
        self.assertEqual(out["overall"]["length"], 1)
        self.assertEqual(out["overall"]["ignored"], 0)

    def test_compare_counts_them_per_side(self):
        A = [s("t1", i, 4, 8) for i in range(1, 5)] + \
            [dict(s("t1", 5, 0, 8), error="HTTPError: 503")]
        B = [s("t1", i, 8, 8) for i in range(1, 5)] + \
            [dict(s("t1", 5, 0, 8), finish="length")]
        r = self.cmp(A, B)
        self.assertEqual(r["ignored"], {"a": 1, "b": 0})
        self.assertEqual(r["length"], {"a": 0, "b": 1})
        row = r["tasks"][0]
        self.assertEqual(row["n_a"], 4)                        # error line not compared
        self.assertEqual(row["n_b"], 5)
        self.assertEqual(row["mean_a"], 50.0)                  # NOT dragged to 40 by a 0
        self.assertEqual(row["mean_b"], 80.0)                  # the cut answer does count


class ChanceTests(unittest.TestCase):
    """The verdict needs BOTH signals: a small permutation p AND a 95% range
    that excludes 0 (see benchstats._joint_verdict). A permutation counts when
    its gap is AT LEAST AS BIG as the observed one - ties count, and bench
    scores tie constantly (100 vs 100, 11/11), so identical data must give
    p ~ 1.0, not the floor 1/(n_perm+1)."""

    def cmp(self, a, b, **kw):
        return benchstats.compare(a, b, n_boot=500, n_perm=2000, seed=3, **kw)

    def test_identical_data_gives_p_one(self):
        for scores in ([100, 100, 100], [100, 0, 100]):     # flat and spiky
            def run():
                return [s(t, i, int(sc), 100)
                        for t in ("h3", "h5") for i, sc in enumerate(scores, 1)]
            r = self.cmp(run(), run())
            self.assertGreater(r["overall"]["p"], 0.99)
            for row in r["tasks"]:
                self.assertGreater(row["p"], 0.99)
            self.assertEqual(r["overall"]["verdict"], "no real difference")

    def test_ties_with_one_odd_sample_still_boring(self):
        """The real case from the live board: two runs of the same setup,
        h5 a perfect 100,100,100 tie in both, h3 off by one sample."""
        A = ([s("h3", i, 100, 100) for i in (1, 2, 3)] +
             [s("h5", i, 100, 100) for i in (1, 2, 3)])
        B = ([s("h3", 1, 100, 100), s("h3", 2, 2, 11), s("h3", 3, 100, 100)] +
             [s("h5", i, 100, 100) for i in (1, 2, 3)])   # 2/11 = 18.18 pts
        r = self.cmp(A, B)
        by = {row["task"]: row for row in r["tasks"]}
        self.assertGreater(by["h5"]["p"], 0.99)            # every permutation ties
        self.assertGreater(r["overall"]["p"], 0.3)
        self.assertEqual(r["overall"]["verdict"], "no real difference")

    def test_clear_separation_is_still_significant(self):
        """Counting ties must not blunt the test: a real gap still lands far
        under alpha (only 1 in 924^3 permutations splits all 3 tasks back)."""
        A = [s(t, i, 0, 100) for t in ("t1", "t2", "t3") for i in range(1, 7)]
        B = [s(t, i, 100, 100) for t in ("t1", "t2", "t3") for i in range(1, 7)]
        r = self.cmp(A, B)
        self.assertLess(r["overall"]["p"], 0.01)
        self.assertEqual(r["overall"]["verdict"], "better")

    def test_p_is_reported_for_overall_and_every_row(self):
        A = CompareTests().two_task_run(lambda t, i: 3 + (i % 3))
        B = CompareTests().two_task_run(lambda t, i: 8 if t == "t1" else 6)
        # the speed row needs a real speed difference to have a small p:
        # identical decode_tps in both runs now correctly gives p ~ 1.0
        # (every permutation ties), which is what used to hide the tie bug
        for i, row in enumerate(A):
            row["decode_tps"] = 18.0 + i % 3
        for i, row in enumerate(B):
            row["decode_tps"] = 26.0 + i % 3
        r = self.cmp(A, B)
        self.assertLess(r["overall"]["p"], 0.05)
        for row in r["tasks"]:
            self.assertLessEqual(row["p"], 0.05)
        self.assertLess(r["speed"]["decode_tps"]["p"], 0.05)

    def test_identical_speed_gives_boring_speed_p(self):
        A = CompareTests().two_task_run(lambda t, i: 3 + (i % 3))   # tps default 20 both sides
        r = self.cmp(A, list(A))
        self.assertGreater(r["speed"]["decode_tps"]["p"], 0.99)
        self.assertEqual(r["speed"]["decode_tps"]["verdict"], "no real difference")

    def test_same_twice_has_a_boring_p(self):
        A = CompareTests().two_task_run(lambda t, i: (4 if i % 2 else 8) if t == "t1" else 3)
        r = self.cmp(A, list(A))
        self.assertEqual(r["overall"]["diff"], 0.0)
        self.assertGreater(r["overall"]["p"], 0.05)            # nothing to see here
        self.assertEqual(r["overall"]["verdict"], "no real difference")

    def test_rows_with_too_few_samples_say_so(self):
        A = CompareTests().two_task_run(lambda t, i: 8 if t == "t1" else 6)
        B = [s("t1", i, 8, 8) for i in range(1, 6)] + [s("t2", 1, 6, 6), s("t2", 2, 6, 6)]
        r = self.cmp(A, B)
        self.assertEqual(r["overall"]["verdict"], "not enough samples")
        by = {row["task"]: row for row in r["tasks"]}
        self.assertEqual(by["t2"]["verdict"], "not enough samples")
        self.assertEqual(by["t1"]["verdict"], "no real difference")


class SpecDiffTests(unittest.TestCase):
    def base(self):
        return {"tasks": ["p1_expr", "h2_cron"], "samples": 5, "max_tokens": 16384}

    def test_token_limit_change_is_found(self):
        d = benchstats.spec_diff(self.base(), dict(self.base(), max_tokens=32768))
        self.assertIn(("max_tokens", "16384", "32768"), d)
        self.assertEqual(len(d), 1)                            # nothing else invented

    def test_samples_and_task_list(self):
        d = benchstats.spec_diff(self.base(), dict(self.base(), samples=8,
                                                   tasks=["p1_expr", "h5_puzzle"]))
        self.assertIn(("samples", "5", "8"), d)
        self.assertIn(("tasks", "p1_expr, h2_cron", "p1_expr, h5_puzzle"), d)

    def test_identical_specs_give_nothing(self):
        self.assertEqual(benchstats.spec_diff(self.base(), dict(self.base())), [])

    def test_missing_spec_is_a_difference(self):
        self.assertTrue(benchstats.spec_diff(None, self.base()))


class CalibrationTests(unittest.TestCase):
    """Measured behaviour of the verdict rules, the way the old plain-bootstrap
    rules were measured by hand: the same model twice must rarely be called a
    change, and a real change must be caught. Scores are drawn in whole test
    counts out of 20, which is as lumpy as real bench data."""

    SCORES = [0, 25, 50, 60, 80, 100]                          # plausible % per task
    TASKS, N = 5, 6

    def run_of(self, rng, shift=0.0):
        out = []
        for t in range(self.TASKS):
            for i in range(1, self.N + 1):
                pct = min(100.0, rng.choice(self.SCORES) + shift)
                out.append(s(f"t{t}", i, round(pct / 100 * 20), 20))
        return out

    def verdicts(self, trials, shift=0.0, base_shift=0.0, seed=11):
        out = []
        for k in range(trials):
            rng = random.Random(seed + k)
            a = self.run_of(rng, base_shift)
            b = self.run_of(rng, base_shift + shift)
            out.append(benchstats.compare(a, b, n_boot=400, n_perm=400,
                                          seed=seed + k)["overall"]["verdict"])
        return out

    def test_same_model_twice_is_rarely_called_a_change(self):
        v = self.verdicts(200)                                  # 200 A/B of identical runs
        flagged = sum(1 for x in v if x in ("better", "worse"))
        # measured here: 5/200 = 2.5% (and 10/200 at 8 samples). The old plain
        # percentile bootstrap flagged 23/200 = 11.5% at 6 samples, which is
        # what the review measured - the ceiling below keeps it from drifting back.
        self.assertLessEqual(flagged, 18, f"{flagged}/200 verdicts said better/worse")
        self.assertEqual([x for x in v if x == "not enough samples"], [])

    def test_a_big_gain_is_caught(self):
        v = self.verdicts(50, shift=50.0)                       # measured: 50/50
        self.assertGreaterEqual(sum(1 for x in v if x == "better"), 45,
                                f"only {sum(1 for x in v if x == 'better')}/50 caught")

    def test_a_middling_gain_is_usually_caught(self):
        # 5 tasks x 6 samples catches a +30 point gain 39/50 times: real but
        # not certain, which is exactly why the pages say "no real difference"
        # instead of guessing.
        v = self.verdicts(50, shift=30.0)
        self.assertGreaterEqual(sum(1 for x in v if x == "better"), 30,
                                f"only {sum(1 for x in v if x == 'better')}/50 caught")


class EnvDiffTests(unittest.TestCase):
    def base(self):
        return {"model": "m1", "n_ctx": 262144, "basalt_commit": "abc123",
                "config": {"models.0.max-context": 262144, "parallel": 4}}

    def test_changed_max_context_found(self):
        b = self.base()
        b = dict(b, config=dict(b["config"], **{"models.0.max-context": 131072}))
        d = benchstats.env_diff(self.base(), b)
        self.assertIn(("models.0.max-context", "262144", "131072"), d)
        self.assertEqual(len(d), 1)

    def test_model_nctx_commit(self):
        d = benchstats.env_diff(self.base(),
                                dict(self.base(), model="m2", n_ctx=32768, basalt_commit="def456"))
        self.assertEqual({k for k, _, _ in d}, {"model", "n_ctx", "basalt_commit"})

    def test_identical_env_empty(self):
        self.assertEqual(benchstats.env_diff(self.base(), dict(self.base())), [])

    def test_added_key_and_missing_env(self):
        d = benchstats.env_diff(self.base(), dict(self.base(),
                                                  config={"models.0.max-context": 262144,
                                                          "parallel": 4, "flash-attn": True}))
        self.assertEqual(d, [("flash-attn", "", "True")])
        self.assertTrue(benchstats.env_diff(None, self.base()))


if __name__ == "__main__":
    unittest.main()
