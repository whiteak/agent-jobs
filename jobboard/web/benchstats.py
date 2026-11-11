#!/usr/bin/env python3
"""benchstats: the maths behind the bench auto-runs (pure functions, stdlib only).

Scores are 0-100 points per sample (passed/total). A run's overall score is the
mean of its per-task means, so every task counts the same no matter how many
hidden tests it has.

  summarize(samples)                 one run: per-task and overall mean/spread
  compare(a, b, n_boot, n_perm, seed)
                                     A/B: per task + overall. The 95% range
                                     comes from a stratified bootstrap (each
                                     task's samples resampled with replacement,
                                     separately for A and B, each resampled
                                     task mean widened around the observed mean
                                     by sqrt(n/(n-1)) - the plain bootstrap of
                                     a handful of points is too tight). The
                                     p-value comes from a stratified permutation
                                     test: within each task the A and B samples
                                     are shuffled together and re-split into the
                                     same group sizes; p = fraction of
                                     permutations whose overall gap is at least
                                     as big as the observed one (two-sided).
                                     random.Random(seed) keeps both reproducible.
  spec_diff(spec_a, spec_b)          run settings (max_tokens, samples, tasks)
                                     that differ between two spec.json files
  env_diff(env_a, env_b)             what changed between two env.json snapshots

Because Basalt is not deterministic and a single sample can swing 10-20 points,
a verdict is only "better"/"worse" when BOTH the permutation p is under 0.05
AND the 95% range of the difference sits entirely above/below zero; either one
alone says too little (with several tasks one of them can differ by chance).
Anything else is "no real difference". With fewer than 3 samples per compared
task the numbers are not trustworthy, so that row - and the overall verdict -
becomes "not enough samples" (the numbers are still shown).

Sample lines whose request failed (an "error" value) are never scores: they
are dropped everywhere, and summarize()/compare() report how many were ignored.

No I/O, no side effects at import.
"""
import math
import random
import statistics

BOOT_N = 4000  # bootstrap resamples; 4000 keeps the 95% range stable to ~0.1 pt
PERM_N = 5000  # permutation draws; p never gets smaller than ~1/PERM_N
MIN_SAMPLES = 3  # below this per task the row is "not enough samples"
ALPHA = 0.05   # verdict needs a permutation p under this AND a range past zero


def points(sample):
    """One sample's score in 0-100 points (0 when there was nothing to pass)."""
    try:
        total = float(sample.get("total") or 0)
        passed = float(sample.get("passed") or 0)
    except (TypeError, ValueError):
        return 0.0
    return 100.0 * passed / total if total > 0 else 0.0


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _percentile(xs, p):
    """p in 0..1, linear interpolation between the closest ranks."""
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo_i = int(k)
    hi_i = min(lo_i + 1, len(xs) - 1)
    return xs[lo_i] + (xs[hi_i] - xs[lo_i]) * (k - lo_i)


def _num(sample, key):
    v = sample.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def _avg_metric(samples, key):
    vals = [v for v in (_num(s, key) for s in samples) if v is not None]
    return _mean(vals) if vals else None


def by_task(samples):
    """{task: [sample, ...]} keeping file order. Malformed entries are skipped,
    and so are sample lines whose request failed (an "error" value): a failed
    request is not a score, and grading it 0 would invent a regression out of a
    server hiccup."""
    out = {}
    for s in samples or []:
        if isinstance(s, dict) and s.get("task") and not s.get("error"):
            out.setdefault(str(s["task"]), []).append(s)
    return out


def error_count(samples):
    """Sample lines whose request failed - ignored everywhere, reported here."""
    return sum(1 for s in (samples or []) if isinstance(s, dict) and s.get("error"))


def length_count(samples):
    """Answers cut off by the token limit (finish == "length"). These ARE real
    scores (the task is graded on what came out), but the count is worth
    showing: a truncated answer is not the same experiment as a finished one."""
    return sum(1 for s in (samples or [])
               if isinstance(s, dict) and s.get("finish") == "length")


def bootstrap_overall_sd(task_score_groups, n_boot=BOOT_N, seed=0):
    """Spread (sd) of the overall mean for one run, by resampling each task's
    samples with replacement, recomputing task means and the mean of them, and
    taking the sd of those draws. The per-sample-index spread of the overall
    score would be meaningless when tasks have different sample counts, which is
    exactly what a partial/stopped run has."""
    groups = [g for g in task_score_groups if g]
    if not groups or n_boot < 2:
        return 0.0
    rng = random.Random(seed)
    draws = [_mean([_mean(rng.choices(g, k=len(g))) for g in groups]) for _ in range(n_boot)]
    return statistics.stdev(draws)


def summarize(samples):
    """{task: {n, mean, sd, min, max, scores, decode_tps_mean, secs_mean},
    "overall": {...}} - means in 0-100 points.

    overall.mean is the mean of the task means (each task weighted equally);
    overall.sd is the bootstrap sd of that mean (see bootstrap_overall_sd);
    overall.min/max bound every individual sample score; overall.scores holds
    the per-task means (there is no honest per-sample overall score).
    """
    tasks = by_task(samples)
    out = {}
    for t in sorted(tasks):
        scores = [points(s) for s in tasks[t]]
        out[t] = {"n": len(scores),
                  "mean": _mean(scores),
                  "sd": statistics.stdev(scores) if len(scores) > 1 else 0.0,
                  "min": min(scores), "max": max(scores),
                  "scores": scores,
                  "decode_tps_mean": _avg_metric(tasks[t], "decode_tps"),
                  "secs_mean": _avg_metric(tasks[t], "total_s")}
    all_scores = [v for t in sorted(out) for v in out[t]["scores"]]
    task_means = [out[t]["mean"] for t in sorted(out)]
    all_samples = [s for t in sorted(tasks) for s in tasks[t]]
    out["overall"] = {"n": len(all_samples),
                      "mean": _mean(task_means),
                      "sd": bootstrap_overall_sd([out[t]["scores"] for t in sorted(out)]),
                      "min": min(all_scores) if all_scores else 0.0,
                      "max": max(all_scores) if all_scores else 0.0,
                      "scores": task_means,
                      "decode_tps_mean": _avg_metric(all_samples, "decode_tps"),
                      "secs_mean": _avg_metric(all_samples, "total_s"),
                      "ignored": error_count(samples),   # lines not scored (see by_task)
                      "length": length_count(samples)}   # answers cut off by max_tokens
    return out


def _joint_verdict(lo, hi, p, up_word, down_word):
    """better/worse needs BOTH signals: a small permutation p (the gap is
    unlikely to be pure luck) AND a 95% range entirely past zero (the gap
    survives the sampling noise). Anything else: no real difference."""
    if lo is None or hi is None or p is None:
        return "no real difference"
    if p < ALPHA and lo > 0:
        return up_word
    if p < ALPHA and hi < 0:
        return down_word
    return "no real difference"


def _widen(resampled_mean, observed_mean, n):
    """Widen one resampled task mean around the observed mean by sqrt(n/(n-1)).
    A bootstrap built from n points has deviations ~sqrt((n-1)/n) too small, so
    its 95% range is too narrow and says "better"/"worse" too easily; this is
    the standard small-sample correction. n=1 has no spread to widen."""
    if n < 2:
        return observed_mean
    return observed_mean + (resampled_mean - observed_mean) * math.sqrt(n / (n - 1.0))


def _perm_p(va, vb, tasks, n_perm, rng):
    """Stratified permutation test, two-sided. For every permutation, within
    EACH task the A and B samples are shuffled together and re-split into the
    same group sizes (the task keeps its n_a/n_b); the task means and the
    overall mean of task means are recomputed, and the permutation counts when
    its overall gap is at least as big as the observed one. p = (hits+1)/(n+1),
    so p never reaches 0. The stratification matters: pooling the tasks would
    let a task with more samples dominate, which is not how the score works."""
    if n_perm < 1 or not tasks:
        return None
    obs = _mean([_mean(vb[t]) - _mean(va[t]) for t in tasks])
    # count ties too: bench scores tie constantly (100 vs 100, 11/11), and a
    # permuted gap equal to the observed one is by definition "at least as
    # big". The epsilon absorbs float noise so equal means really tie.
    limit = abs(obs) - 1e-9
    prep = []
    for t in tasks:
        na, nb = len(va[t]), len(vb[t])
        pool = list(va[t]) + list(vb[t])
        prep.append((pool, na, nb, sum(pool)))
    hits = 0
    for _ in range(n_perm):
        d = 0.0
        for pool, na, nb, tot in prep:
            sa = sum(rng.sample(pool, na))
            d += (tot - sa) / nb - sa / na
        if abs(d / len(prep)) >= limit:
            hits += 1
    return (hits + 1) / (n_perm + 1)


def _group_stats(va, vb, tasks, n_boot, n_perm, rng):
    """{task: {lo, hi, p}} plus key None for the overall (mean of task means).
    The ranges come from the widened stratified bootstrap, the p values from
    the stratified permutation test; both from the same seeded rng."""
    stats = {t: {"lo": None, "hi": None, "p": None} for t in tasks}
    if not tasks:
        stats[None] = {"lo": None, "hi": None, "p": None}
        return stats
    obs_a = {t: _mean(va[t]) for t in tasks}
    obs_b = {t: _mean(vb[t]) for t in tasks}
    diff_draws = {t: [] for t in tasks}
    overall_draws = []
    for _ in range(n_boot):
        ma, mb = [], []
        for t in tasks:
            a_m = _widen(_mean([va[t][rng.randrange(len(va[t]))] for _ in va[t]]),
                         obs_a[t], len(va[t]))
            b_m = _widen(_mean([vb[t][rng.randrange(len(vb[t]))] for _ in vb[t]]),
                         obs_b[t], len(vb[t]))
            diff_draws[t].append(b_m - a_m)
            ma.append(a_m)
            mb.append(b_m)
        overall_draws.append(_mean(mb) - _mean(ma))
    for t in tasks:
        stats[t] = {"lo": _percentile(diff_draws[t], 0.025),
                    "hi": _percentile(diff_draws[t], 0.975),
                    "p": _perm_p({t: va[t]}, {t: vb[t]}, [t], n_perm, rng)}
    stats[None] = {"lo": _percentile(overall_draws, 0.025),
                   "hi": _percentile(overall_draws, 0.975),
                   "p": _perm_p(va, vb, tasks, n_perm, rng)}
    return stats


def compare(a_samples, b_samples, n_boot=BOOT_N, n_perm=PERM_N, seed=0):
    """Compare two runs over the tasks both have. diff is B - A in points, so a
    positive diff means the B ("after") run scored higher. Sample lines with an
    "error" are ignored (counted in res["ignored"]).

    Returns {"common": [...], "tasks": [row...], "overall": row|None,
             "speed": {"decode_tps": row, "secs": row},
             "ignored": {"a": n, "b": n}, "length": {"a": n, "b": n}}.
    Each score row: {task?, n_a, n_b, mean_a, mean_b, diff, lo, hi, p, verdict}.
    Speed rows use the same method and get the same verdicts, only worded
    faster/slower, and are never part of the score verdict.
    """
    A, B = by_task(a_samples), by_task(b_samples)
    common = sorted(set(A) & set(B))
    res = {"common": common, "tasks": [], "overall": None, "speed": {},
           "ignored": {"a": error_count(a_samples), "b": error_count(b_samples)},
           "length": {"a": length_count(a_samples), "b": length_count(b_samples)}}
    if not common:
        return res

    sc_a = {t: [points(s) for s in A[t]] for t in common}
    sc_b = {t: [points(s) for s in B[t]] for t in common}
    dec_a = {t: [v for v in (_num(s, "decode_tps") for s in A[t]) if v is not None]
             for t in common}
    dec_b = {t: [v for v in (_num(s, "decode_tps") for s in B[t]) if v is not None]
             for t in common}
    sec_a = {t: [v for v in (_num(s, "total_s") for s in A[t]) if v is not None]
             for t in common}
    sec_b = {t: [v for v in (_num(s, "total_s") for s in B[t]) if v is not None]
             for t in common}

    rng = random.Random(seed)
    n_boot = max(2, int(n_boot))
    n_perm = max(0, int(n_perm))
    sc_stats = _group_stats(sc_a, sc_b, common, n_boot, n_perm, rng)

    def score_row(task, st):
        if task is None:
            vals_a = [v for t in common for v in sc_a[t]]
            vals_b = [v for t in common for v in sc_b[t]]
            mean_a = _mean([_mean(sc_a[t]) for t in common])   # mean of task means
            mean_b = _mean([_mean(sc_b[t]) for t in common])
            row = {"n_a": len(vals_a), "n_b": len(vals_b)}
            few = any(len(sc_a[t]) < MIN_SAMPLES or len(sc_b[t]) < MIN_SAMPLES
                      for t in common)
        else:
            vals_a, vals_b = sc_a[task], sc_b[task]
            mean_a, mean_b = _mean(vals_a), _mean(vals_b)
            row = {"task": task, "n_a": len(vals_a), "n_b": len(vals_b)}
            few = len(vals_a) < MIN_SAMPLES or len(vals_b) < MIN_SAMPLES
        lo, hi, p = st["lo"], st["hi"], st["p"]
        row.update({"mean_a": mean_a, "mean_b": mean_b, "diff": mean_b - mean_a,
                    "sd_a": statistics.stdev(vals_a) if len(vals_a) > 1 else 0.0,
                    "sd_b": statistics.stdev(vals_b) if len(vals_b) > 1 else 0.0,
                    "lo": lo, "hi": hi, "p": p,
                    "verdict": "not enough samples" if few
                    else _joint_verdict(lo, hi, p, "better", "worse")})
        return row

    res["tasks"] = [score_row(t, sc_stats[t]) for t in common]
    res["overall"] = score_row(None, sc_stats[None])

    for key, va, vb, up_word, down_word in (("decode_tps", dec_a, dec_b, "faster", "slower"),
                                            ("secs", sec_a, sec_b, "slower", "faster")):
        usable = [t for t in common if va[t] and vb[t]]
        st = _group_stats({t: va[t] for t in usable}, {t: vb[t] for t in usable},
                          usable, n_boot, n_perm, rng)[None]
        vals_a = [v for t in usable for v in va[t]]
        vals_b = [v for t in usable for v in vb[t]]
        mean_a = _mean([_mean(va[t]) for t in usable])         # mean of task means,
        mean_b = _mean([_mean(vb[t]) for t in usable])         # like the score rows
        res["speed"][key] = {"n_a": len(vals_a), "n_b": len(vals_b),
                             "mean_a": mean_a, "mean_b": mean_b,
                             "diff": mean_b - mean_a,
                             "pct": (100.0 * (mean_b - mean_a) / mean_a) if mean_a else None,
                             "lo": st["lo"], "hi": st["hi"], "p": st["p"],
                             "verdict": _joint_verdict(st["lo"], st["hi"], st["p"],
                                                       up_word, down_word)}
    return res


def spec_diff(spec_a, spec_b):
    """(key, a, b) for the run settings that differ between two spec.json files:
    max_tokens, samples and the task list. env.json cannot see these, so without
    this a 16384-vs-32768 max_tokens comparison reads as "nothing changed"."""
    spec_a, spec_b = spec_a or {}, spec_b or {}

    def norm(v):
        return "" if v is None else str(v)

    def tasks(s):
        return ", ".join(str(x) for x in (s.get("tasks") or []))

    out = []
    for key, take in (("max_tokens", lambda s: s.get("max_tokens")),
                      ("samples", lambda s: s.get("samples")),
                      ("tasks", tasks)):
        va, vb = norm(take(spec_a)), norm(take(spec_b))
        if va != vb:
            out.append((key, va, vb))
    return out


def env_diff(env_a, env_b):
    """(key, a, b) for every model id, n_ctx, Basalt commit and (flattened)
    Basalt config key that differs between two env.json snapshots."""
    env_a, env_b = env_a or {}, env_b or {}

    def norm(v):
        return "" if v is None else str(v)

    out = []
    for key in ("model", "n_ctx", "basalt_commit"):
        va, vb = norm(env_a.get(key)), norm(env_b.get(key))
        if va != vb:
            out.append((key, va, vb))
    fa, fb = env_a.get("config") or {}, env_b.get("config") or {}
    for key in sorted(set(fa) | set(fb)):
        if norm(fa.get(key)) != norm(fb.get(key)):
            out.append((key, norm(fa.get(key)), norm(fb.get(key))))
    return out
