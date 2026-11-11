"""Local LLM benchmark: real coding work, graded by hidden tests, plus speed.

Runs the exact same suite against any OpenAI-compatible server (Basalt, LM Studio, llama.cpp, ...):

  code     6 hard programming problems. The model's code is run against hidden unit tests.
  bugfix   a 90-line module with 6 user-reported bugs; the fixed file is run against hidden tests.
  longctx  facts buried at ~10/30/50/90% of a ~30K and a ~100K token codebase; the answer must
           combine them (one fact overrides an earlier one, one needs arithmetic).
  agent    Qwen Code edits a small repo from a task brief (6 features); graded by hidden tests.

Usage:
  py -3.12 llm_bench.py run --label NAME [--url http://127.0.0.1:8080/v1] [--model ID]
                            [--reps 2] [--only code,bugfix,longctx,agent] [--qwen-model ID]
  py -3.12 llm_bench.py validate        # checks every hidden test against the reference solutions
  py -3.12 llm_bench.py compare results/A.json results/B.json ...

Every request: temperature 0.6, top_p 0.95, seed 1, max_tokens 16384 (thinking allowed).
Model-written code runs locally in a temp folder with a timeout. Standard library only.
"""
import argparse, datetime, glob, hashlib, json, os, platform, re, shutil, statistics, subprocess, sys
import tempfile, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SUITE = os.path.join(HERE, "suite")
BENCH_VERSION = 2
SAMPLING = {"temperature": 0.6, "top_p": 0.95, "seed": 1}
MAX_TOKENS = 16384
PY = sys.executable


# ---------------------------------------------------------------- server

def chat(url, model, prompt, max_tokens=MAX_TOKENS, timeout=3600):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
            "stream": True, "stream_options": {"include_usage": True}, **SAMPLING}
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer local"})
    t0 = time.perf_counter(); t_first = t_last = None; usage = None; n = 0
    content, reasoning, finish = [], [], None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                ev = json.loads(data)
                if ev.get("usage"):
                    usage = ev["usage"]
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    c = d.get("content") or ""
                    rs = d.get("reasoning_content") or d.get("reasoning") or ""
                    if c or rs:
                        now = time.perf_counter()
                        t_first = t_first or now; t_last = now; n += 1
                        content.append(c); reasoning.append(rs)
                    finish = ch.get("finish_reason") or finish
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:500], "content": "", "total_s": round(time.perf_counter() - t0, 2)}
    t_end = time.perf_counter()
    text = "".join(content)
    m = re.search(r"</think>", text)
    if m:  # servers that leave the thinking in the content
        reasoning.append(text[:m.end()]); text = text[m.end():]
    pt = (usage or {}).get("prompt_tokens"); ct = (usage or {}).get("completion_tokens") or n
    ttft = (t_first - t0) if t_first else None
    gen = (t_last - t_first) if t_first and t_last and t_last > t_first else None
    return {"content": text, "reasoning_chars": sum(map(len, reasoning)), "finish": finish,
            "prompt_tokens": pt, "completion_tokens": ct,
            "ttft_s": round(ttft, 3) if ttft else None,
            "prefill_tps": round(pt / ttft, 1) if pt and ttft else None,
            "decode_tps": round((ct - 1) / gen, 1) if gen and ct > 1 else None,
            "total_s": round(t_end - t0, 2)}


def server_model(url):
    with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=30) as r:
        return json.load(r)["data"][0]["id"]


# ---------------------------------------------------------------- grading

def extract_code(text):
    blocks = re.findall(r"```(?:python|py)?[ \t]*\n(.*?)```", text, re.S)
    if not blocks:
        return None
    return max(blocks, key=len)


def run_tests(test_file, files, args=(), cwd=None, timeout=300):
    """Copy test + files into a temp dir, run unittest, return (passed, total, tail)."""
    d = tempfile.mkdtemp(prefix="llmb_")
    try:
        shutil.copy(test_file, os.path.join(d, "test_hidden.py"))
        for name, src in files.items():
            if isinstance(src, str) and os.path.isfile(src):
                shutil.copy(src, os.path.join(d, name))
            else:
                open(os.path.join(d, name), "w", encoding="utf-8").write(src)
        try:
            p = subprocess.run([PY, "-E", "-s", "test_hidden.py", *args, "-v"], cwd=cwd or d,
                               capture_output=True, text=True, timeout=timeout,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            out = p.stderr + p.stdout
        except subprocess.TimeoutExpired:
            return 0, None, "TIMEOUT"
        # the grader's own summary is the LAST "Ran N tests" (nested test runs print theirs earlier)
        runs = list(re.finditer(r"Ran (\d+) tests?", out))
        if not runs:
            return 0, None, out[-1500:]
        total = int(runs[-1].group(1))
        rest = out[runs[-1].end():]
        bad = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", rest))
        return max(total - bad, 0), total, out[-1500:]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def count_tests(test_file):
    return len(re.findall(r"^\s+def test_", open(test_file, encoding="utf-8").read(), re.M))


# ---------------------------------------------------------------- tasks

def code_tasks():
    for d in sorted(glob.glob(os.path.join(SUITE, "code", "*"))):
        extra = {"reference_check.py": os.path.join(d, "reference_check.py")} if os.path.exists(
            os.path.join(d, "reference_check.py")) else {}
        yield os.path.basename(d), open(os.path.join(d, "prompt.md"), encoding="utf-8").read(), \
            os.path.join(d, "test_hidden.py"), extra, os.path.join(d, "reference.py")


def bugfix_tasks():
    for d in sorted(glob.glob(os.path.join(SUITE, "bugfix", "*"))):
        prompt = open(os.path.join(d, "prompt_head.md"), encoding="utf-8").read() + \
            open(os.path.join(d, "buggy.py"), encoding="utf-8").read() + "```\n"
        yield os.path.basename(d), prompt, os.path.join(d, "test_hidden.py"), {}, os.path.join(d, "reference.py")


NEEDLES = [
    (0.10, 'MAINT_WINDOW_OPENS_UTC = "02:40"  # nightly maintenance window start, agreed with infra\n'),
    (0.30, "MAINT_WINDOW_LENGTH_MIN = 60  # provisional length of the nightly maintenance window\n"),
    (0.50, "MAINT_WINDOW_LENGTH_MIN = 95  # final: overrides the provisional 60-minute value set earlier\n"),
    (0.90, 'MAINT_ON_CALL_ALIAS = "kestrel-7"  # who to page during the maintenance window\n'),
]
LONGCTX_Q = ("\n\nSomewhere in the code above there are settings for a nightly maintenance window. Using the FINAL "
             "values, answer: at what UTC time (HH:MM) does the maintenance window close, and what is the on-call "
             "alias to page? Answer in one line exactly like: close=HH:MM alias=NAME")
LONGCTX_SIZES = {"longctx_30k": 100_000, "longctx_100k": 330_000}


def longctx_prompt(chars, rep=0):
    """rep > 0 puts a different first line on the prompt so a server's prompt cache cannot reuse it."""
    src =open(os.path.join(SUITE, "longctx", "corpus_full.py"), encoding="utf-8").read()[:chars]
    src = src[:src.rfind("\n") + 1]
    lines = src.splitlines(keepends=True)
    for frac, needle in sorted(NEEDLES, reverse=True):
        lines.insert(int(len(lines) * frac), needle)
    return f"[benchmark request {rep + 1}] Here is the source code of a project:\n\n```python\n" + "".join(lines) + "```" + LONGCTX_Q


def grade_longctx(text):
    close = re.search(r"close\s*=\s*(\d{1,2}:\d\d)", text or "")
    alias = re.search(r"alias\s*=\s*\"?([\w-]+)", text or "")
    pts = (1 if close and close.group(1).zfill(5) == "04:15" else 0) + (1 if alias and alias.group(1) == "kestrel-7" else 0)
    return pts, 2


AGENT_DIR = os.path.join(SUITE, "agent", "a1_textstats")


def run_agent(label, qwen_model, timeout=2700):
    ws = os.path.join(HERE, "results", "agent_ws", f"{label}_{datetime.datetime.now():%Y%m%d-%H%M%S}")
    shutil.copytree(os.path.join(AGENT_DIR, "repo"), ws)
    shutil.copy(os.path.join(AGENT_DIR, "task.md"), os.path.join(ws, "TASK.md"))
    qwen = shutil.which("qwen") or shutil.which("qwen.cmd")
    if not qwen:
        return {"error": "qwen not found", "passed": 0, "total": count_tests(os.path.join(AGENT_DIR, "test_hidden.py"))}
    cmd = [qwen, "-p", "Implement everything in TASK.md in this repository.", "--approval-mode", "auto-edit"]
    if qwen_model:
        cmd[1:1] = ["-m", qwen_model]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                           errors="replace")
        log = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        log = "TIMEOUT"
    secs = round(time.perf_counter() - t0, 1)
    open(os.path.join(ws, "_agent_log.txt"), "w", encoding="utf-8").write(log)
    passed, total, tail = run_tests(os.path.join(AGENT_DIR, "test_hidden.py"), {}, args=(ws,))
    total = total or count_tests(os.path.join(AGENT_DIR, "test_hidden.py"))
    return {"workspace": ws, "seconds": secs, "passed": passed, "total": total, "tail": tail[-800:]}


# ---------------------------------------------------------------- commands

def cmd_validate(_):
    ok = True
    for name, _, test, extra, ref in list(code_tasks()) + list(bugfix_tasks()):
        p, t, tail = run_tests(test, {"solution.py": ref, **extra})
        print(f"{name:16s} reference {p}/{t}"); ok &= bool(p == t and t)
        if p != t:
            print(tail)
    for name, _, test, extra, _ in bugfix_tasks():
        p, t, _ = run_tests(test, {"solution.py": os.path.join(SUITE, "bugfix", name, "buggy.py")})
        print(f"{name:16s} buggy     {p}/{t} (must be below full)"); ok &= bool(p != t)
    p, t, tail = run_tests(os.path.join(AGENT_DIR, "test_hidden.py"), {}, args=(os.path.join(AGENT_DIR, "reference_repo"),))
    print(f"{'agent ref':16s} reference {p}/{t}"); ok &= bool(p == t and t)
    if p != t:
        print(tail)
    p, t, _ = run_tests(os.path.join(AGENT_DIR, "test_hidden.py"), {}, args=(os.path.join(AGENT_DIR, "repo"),))
    print(f"{'agent start':16s} untouched {p}/{t} (must be below full)")
    for k, chars in LONGCTX_SIZES.items():
        print(f"{k:16s} prompt {len(longctx_prompt(chars)):,} chars")
    print("VALID" if ok else "INVALID")
    return 0 if ok else 1


def cmd_run(a):
    model = a.model or server_model(a.url)
    only = set(a.only.split(",")) if a.only else {"code", "bugfix", "longctx", "agent"}
    print(f"server {a.url}  model {model}  label {a.label}  reps {a.reps}  sections {sorted(only)}", flush=True)
    chat(a.url, model, "Reply with OK.", 64)  # warm-up, not counted
    res = {"bench_version": BENCH_VERSION, "label": a.label, "url": a.url, "model": model, "notes": a.notes,
           "date": datetime.datetime.now().isoformat(timespec="seconds"), "host": platform.node(),
           "sampling": SAMPLING, "max_tokens": MAX_TOKENS, "tasks": {}, "agent": None}
    started = time.perf_counter()
    out_dir = os.path.join(HERE, "results", "answers", a.label); os.makedirs(out_dir, exist_ok=True)

    graded = []
    if "code" in only:
        graded += [("code",) + t for t in code_tasks()]
    if "bugfix" in only:
        graded += [("bugfix",) + t for t in bugfix_tasks()]
    for kind, name, prompt, test, extra, _ in graded:
        runs = []
        for i in range(a.reps):
            r = chat(a.url, model, prompt)
            code = extract_code(r.get("content", ""))
            if code:
                p, t, tail = run_tests(test, {"solution.py": code, **extra})
                t = t or count_tests(test)
            else:
                p, t, tail = 0, count_tests(test), "no code block"
            r.update(passed=p, total=t, test_tail=tail[-600:])
            open(os.path.join(out_dir, f"{name}_r{i+1}.md"), "w", encoding="utf-8").write(r.pop("content", ""))
            runs.append(r)
            print(f"  {name:14s} r{i+1}: tests {p}/{t}  tg={r.get('completion_tokens')} "
                  f"decode={r.get('decode_tps')} t/s  {r.get('total_s')}s {r.get('error', '')}", flush=True)
        res["tasks"][name] = {"kind": kind, "runs": runs}

    if "longctx" in only:
        for name, chars in LONGCTX_SIZES.items():
            runs = []
            for i in range(a.reps):
                r = chat(a.url, model, longctx_prompt(chars, i))
                p, t = grade_longctx(r.get("content", ""))
                r.update(passed=p, total=t, answer=(r.pop("content", "") or "")[-300:])
                runs.append(r)
                print(f"  {name:14s} r{i+1}: score {p}/{t}  pp={r.get('prompt_tokens')} ttft={r.get('ttft_s')}s "
                      f"prefill={r.get('prefill_tps')} t/s  {r.get('error', '')}", flush=True)
            res["tasks"][name] = {"kind": "longctx", "runs": runs}

    if "agent" in only:
        print("  agent: Qwen Code working on the textstats repo ...", flush=True)
        ag = run_agent(a.label, a.qwen_model or model)
        res["agent"] = ag
        print(f"  agent: tests {ag['passed']}/{ag['total']} in {ag.get('seconds')}s  {ag.get('error', '')}", flush=True)

    res["wall_s"] = round(time.perf_counter() - started, 1)
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    path = os.path.join(HERE, "results", f"{a.label}_{datetime.datetime.now():%Y%m%d-%H%M}.json")
    json.dump(res, open(path, "w", encoding="utf-8"), indent=2)
    print(f"\nsaved {path}\n\n{table([res])}")


def summary(r):
    s = {}
    by_kind = {}
    for name, t in r["tasks"].items():
        for run in t["runs"]:
            k = by_kind.setdefault(t["kind"], [0, 0, 0, 0])
            k[0] += run.get("passed") or 0; k[1] += run.get("total") or 0
            k[2] += 1 if run.get("total") and run.get("passed") == run.get("total") else 0; k[3] += 1
    for kind, (p, t, full, n) in by_kind.items():
        s[kind] = f"{100 * p / t:.0f}% tests, {full}/{n} fully solved" if t else "-"
    if r.get("agent"):
        ag = r["agent"]; s["agent"] = f"{ag['passed']}/{ag['total']} tests, {ag.get('seconds', '-')} s"
    runs = [x for t in r["tasks"].values() for x in t["runs"]]
    dec = [(x["completion_tokens"], x["completion_tokens"] / x["decode_tps"]) for x in runs
           if x.get("decode_tps") and x.get("completion_tokens")]
    s["decode t/s"] = f"{sum(a for a, _ in dec) / sum(b for _, b in dec):.1f}" if dec else "-"
    lc = [x for t in r["tasks"].values() if t["kind"] == "longctx" for x in t["runs"] if x.get("prefill_tps")]
    s["prefill t/s (long)"] = f"{statistics.median(x['prefill_tps'] for x in lc):.0f}" if lc else "-"
    tg = sum(x.get("completion_tokens") or 0 for x in runs)
    s["tokens generated"] = f"{tg:,}"
    s["wall time"] = f"{r.get('wall_s', 0) / 60:.1f} min"
    return s


def table(results):
    sums = [summary(r) for r in results]
    keys = ["code", "bugfix", "longctx", "agent", "decode t/s", "prefill t/s (long)", "tokens generated", "wall time"]
    lines = ["| | " + " | ".join(r["label"] for r in results) + " |", "| --- |" + " --- |" * len(results)]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(s.get(k, "-") for s in sums) + " |")
    names = sorted({n for r in results for n in r["tasks"]})
    lines += ["", "| task | " + " | ".join(r["label"] for r in results) + " |", "| --- |" + " --- |" * len(results)]
    for n in names:
        cells = []
        for r in results:
            t = r["tasks"].get(n)
            cells.append(" ".join(f"{x.get('passed', 0)}/{x.get('total', '?')}" for x in t["runs"]) if t else "-")
        lines.append(f"| {n} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("--url", default="http://127.0.0.1:8080/v1")
    r.add_argument("--label", required=True)
    r.add_argument("--model")
    r.add_argument("--qwen-model", help="model id for Qwen Code (default: the server's model id)")
    r.add_argument("--reps", type=int, default=2)
    r.add_argument("--only")
    r.add_argument("--notes", default="")
    sp.add_parser("validate")
    c = sp.add_parser("compare"); c.add_argument("files", nargs="+")
    a = p.parse_args()
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "validate":
        return cmd_validate(a)
    print(table([json.load(open(f, encoding="utf-8")) for f in a.files]))


if __name__ == "__main__":
    sys.exit(main())
