"""Hard A/B suite: single-shot coding/reasoning tasks graded by hidden tests.

  python3 hard_ab.py run --label NAME [--tasks h1_lisp,h2_cron] [--samples 2] [--url http://127.0.0.1:8080/v1]
  python3 hard_ab.py validate
  python3 hard_ab.py merge OUT.json A.json B.json ...

Sampling as the main suite (temperature 0.6, top_p 0.95, max_tokens 32768 by default (--max-tokens; was 16384 until 2026-11-09)), seed = sample number (1, 2, ...), so
both models get the same seeds. Puzzle tasks (grade.py instead of test_hidden.py) are graded on the final answer.
"""
import argparse, datetime, glob, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import llm_bench as lb  # noqa: E402

TASKS = os.path.join(HERE, "tasks")


def tasks(only=None):
    for d in sorted(glob.glob(os.path.join(TASKS, "*"))):
        name = os.path.basename(d)
        if only and name not in only:
            continue
        yield name, d


def grade(d, content):
    test = os.path.join(d, "test_hidden.py")
    extra = {f: os.path.join(d, f) for f in os.listdir(d) if f.startswith("given_")}
    if os.path.exists(os.path.join(d, "grade.py")):
        p = subprocess.run([sys.executable, os.path.join(d, "grade.py")], input=content, capture_output=True,
                           text=True, timeout=60)
        passed, total = map(int, p.stdout.split()[:2])
        return passed, total, p.stdout[-300:]
    code = lb.extract_code(content)
    if not code:
        return 0, lb.count_tests(test), "no code block"
    p, t, tail = lb.run_tests(test, {"solution.py": code, **extra}, timeout=120)
    return p, t or lb.count_tests(test), tail[-600:]


def cmd_run(a):
    model = a.model or lb.server_model(a.url)
    only = set(a.tasks.split(",")) if a.tasks else None
    res = {"label": a.label, "model": model, "date": datetime.datetime.now().isoformat(timespec="seconds"),
           "sampling": {**lb.SAMPLING, "seed": "sample number"}, "max_tokens": a.max_tokens, "tasks": {}}
    out_dir = os.path.join(HERE, "answers", a.label); os.makedirs(out_dir, exist_ok=True)
    lb.chat(a.url, model, "Reply with OK.", 64)
    for name, d in tasks(only):
        prompt = open(os.path.join(d, "prompt.md"), encoding="utf-8").read()
        runs = []
        for s in range(1, a.samples + 1):
            lb.SAMPLING["seed"] = s
            r = lb.chat(a.url, model, prompt, max_tokens=a.max_tokens)
            content = r.pop("content", "") or ""
            open(os.path.join(out_dir, f"{name}_s{s}.md"), "w", encoding="utf-8").write(content)
            p, t, tail = grade(d, content)
            r.update(seed=s, passed=p, total=t, tail=tail)
            runs.append(r)
            print(f"  {name:12s} s{s}: {p}/{t}  tg={r.get('completion_tokens')} decode={r.get('decode_tps')} "
                  f"t/s  {r.get('total_s')}s finish={r.get('finish')} {r.get('error', '')}", flush=True)
        res["tasks"][name] = runs
    path = a.out or os.path.join(os.path.dirname(HERE), "results", f"hard-{a.label}_{datetime.datetime.now():%Y%m%d-%H%M}.json")
    json.dump(res, open(path, "w"), indent=2)
    print("saved", path)


def cmd_validate(_):
    ok = True
    for name, d in tasks():
        ref = os.path.join(d, "reference.py")
        if os.path.exists(os.path.join(d, "grade.py")):
            content = open(os.path.join(d, "reference_answer.md")).read()
            p, t, _ = grade(d, content)
        else:
            p, t, _ = grade(d, "```python\n" + open(ref).read() + "\n```")
        print(f"{name:12s} reference {p}/{t}"); ok &= p == t
    print("VALID" if ok else "INVALID")


def cmd_merge(a):
    out = None
    for f in a.files:
        d = json.load(open(f))
        if out is None:
            out = d
        else:
            out["tasks"].update(d["tasks"])
    json.dump(out, open(a.out, "w"), indent=2)


def main():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--label", required=True); r.add_argument("--tasks")
    r.add_argument("--samples", type=int, default=2); r.add_argument("--url", default="http://127.0.0.1:8080/v1")
    r.add_argument("--model"); r.add_argument("--out"); r.add_argument("--max-tokens", type=int, default=32768)
    sub.add_parser("validate")
    m = sub.add_parser("merge"); m.add_argument("out"); m.add_argument("files", nargs="+")
    a = ap.parse_args()
    {"run": cmd_run, "validate": cmd_validate, "merge": cmd_merge}[a.cmd](a)


if __name__ == "__main__":
    main()
