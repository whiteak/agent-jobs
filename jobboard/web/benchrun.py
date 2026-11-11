#!/usr/bin/env python3
"""benchrun: run one automatic bench run against the local model server.

  python3 benchrun.py run <run_dir>

<run_dir>/spec.json holds everything: {"id","label","notes","tasks":[ids],
"samples":N,"max_tokens":M,"url":"http://host:port/v1"}. The run writes:

  env.json         snapshot of the setup, once at the start: date, hostname, the
                   server's model id and n_ctx (GET /v1/models: data[0].id and
                   data[0].meta.n_ctx), the Basalt config path, its sha256 and a
                   flattened copy of it (BASALT_CONFIG, default ~/basalt_config.json
                   - read only, never written), and the Basalt git commit
                   (BASALT_DIR, default ~/basalt; "" when that fails).
  samples.jsonl    one JSON line per graded sample, appended IMMEDIATELY, so a
                   stopped or interrupted run can be resumed: pairs already in
                   the file are skipped.
  answers/         the full answer text of every sample as <task>_s<n>.md.
  progress.json    {done, total, current, eta_s} after every sample.

Samples run ROUND-ROBIN: sample 1 of every task, then sample 2, and so on, so
slow drift over hours (heat, adaptive expert placement) hits every task equally
and a stopped run still has balanced data. seed = sample number for every task,
so a before-run and an after-run get the same seeds (the agent task's seed is
not settable).

Grading is NOT reimplemented here: it goes through the existing bench code,
imported as modules from BENCH_DIR (default ~/bench; tests use bench-ref):
llm_bench (chat, code/bugfix tasks, longctx, agent, run_tests) and hard_ab
(hard task grading). Model-written code is executed by llm_bench.run_tests
exactly as the existing bench does (on the host, temp dir, timeout).

A sample whose REQUEST failed is not a score: the (task, sample) pair is retried
instead of being recorded as 0/total. Retry policy: wait 30 s, check GET /models,
try again; the waits are 30, 60, 120 s (first try + 3 retries). If every attempt
failed, the run pauses: progress.json says so, nothing at all is appended for
that sample, and the run exits 3 so the board can offer Resume once the server
is back. Override the waits with BENCH_RETRY_WAITS="1,2,3" (used by the tests).
An agent sample that timed out counts as a failed attempt too; a missing qwen
command aborts at once (every agent sample would fail).

Answers cut off by max_tokens (finish == "length") ARE scores and are kept;
benchstats counts them so the pages can say how many answers were truncated.

Exit codes: 0 complete, 143 on SIGTERM (whatever is saved stays saved), 2 when
the server did not answer at the start, 3 when the server stopped answering
mid-run (the run can be resumed), 1 for bad spec/task ids or a missing qwen.
Standard library only.
"""
import argparse, hashlib, json, os, re, signal, socket, subprocess, sys, time, urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8080/v1"


def retry_waits():
    """Seconds to wait before retry attempts 2, 3, 4 (default 30, 60, 120)."""
    raw = os.environ.get("BENCH_RETRY_WAITS")
    if raw is None:
        return [30.0, 60.0, 120.0]
    out = []
    for part in raw.split(","):
        try:
            out.append(max(0.0, float(part)))
        except ValueError:
            continue
    return out


def server_up(url):
    """GET /models with a short timeout - the cheap 'is it back?' probe."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=20):
            return True
    except Exception:
        return False


def bench_dir():
    return os.environ.get("BENCH_DIR") or str(Path.home() / "bench")


def hard_task_ids():
    d = Path(bench_dir()) / "hard" / "tasks"
    try:
        return sorted(p.name for p in d.iterdir() if p.is_dir())
    except OSError:
        return []


# ---------------------------------------------------------------- env snapshot

def flatten(obj, pre=None):
    """{"models":[{"max-context":262144}]} -> {"models.0.max-context": 262144}."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{pre}.{k}" if pre is not None else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{pre}.{i}" if pre is not None else str(i)))
    else:
        out[pre if pre is not None else "value"] = obj
    return out


def env_snapshot(url):
    """Snapshot the setup. Exits 2 with a clear stderr message when the server
    does not answer - a bench run is worthless without knowing what it measured."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=30) as r:
            d = json.load(r)["data"][0]
        model = d.get("id") or ""
        n_ctx = (d.get("meta") or {}).get("n_ctx")
    except Exception as ex:
        sys.stderr.write(f"benchrun: the server at {url} did not answer /models "
                         f"({type(ex).__name__}: {ex}). Start Basalt and try again.\n")
        sys.exit(2)
    cfg_path = Path(os.environ.get("BASALT_CONFIG") or (Path.home() / "basalt_config.json"))
    cfg_flat, cfg_sha = {}, ""
    try:
        raw = cfg_path.read_bytes()
        cfg_sha = hashlib.sha256(raw).hexdigest()
        cfg_flat = flatten(json.loads(raw.decode("utf-8", "replace")))
    except Exception:
        pass  # a missing/unreadable config still leaves a usable snapshot
    basalt_dir = os.environ.get("BASALT_DIR") or str(Path.home() / "basalt")
    try:
        commit = subprocess.run(["git", "-C", basalt_dir, "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        commit = ""
    if not re.fullmatch(r"[0-9a-f]{6,40}", commit):
        commit = ""
    return {"date": time.strftime("%Y-%m-%dT%H:%M:%S"), "hostname": socket.gethostname(),
            "url": url, "model": model, "n_ctx": n_ctx,
            "config_path": str(cfg_path), "config_sha256": cfg_sha, "config": cfg_flat,
            "basalt_commit": commit}


# ---------------------------------------------------------------- task catalog

class Task:
    """One bench task: kind plus whatever the runner needs."""

    def __init__(self, tid, kind, lb, hard_ab=None):
        self.id, self.kind = tid, kind
        if kind == "hard":
            self.dir = str(Path(bench_dir()) / "hard" / "tasks" / tid)
            self.prompt = open(Path(self.dir) / "prompt.md", encoding="utf-8").read()
            self.hard_ab = hard_ab
        elif kind == "code" or kind == "bugfix":
            src = lb.code_tasks() if kind == "code" else lb.bugfix_tasks()
            hit = next((t for t in src if t[0] == tid), None)
            if hit is None:
                raise ValueError(f"no {kind} task {tid} in {bench_dir()}")
            _, self.prompt, self.test, self.extra, _ = hit
        elif kind == "longctx":
            self.chars = lb.LONGCTX_SIZES[tid]
        # agent: run_agent(label, model); no prompt here


def resolve_tasks(spec, lb, hard_ab):
    """Map spec task ids to Task objects. Unknown ids abort the run (exit 1):
    silently skipping them would compare two runs that measured different things."""
    hard_ids = set(hard_task_ids())
    tasks = []
    for tid in spec.get("tasks") or []:
        tid = str(tid)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tid):
            sys.stderr.write(f"benchrun: bad task id {tid!r}\n")
            sys.exit(1)
        if tid in hard_ids:
            kind = "hard"
        elif tid in lb.LONGCTX_SIZES:
            kind = "longctx"
        elif tid == "agent":
            kind = "agent"
        elif any(t[0] == tid for t in lb.code_tasks()):
            kind = "code"
        elif any(t[0] == tid for t in lb.bugfix_tasks()):
            kind = "bugfix"
        else:
            sys.stderr.write(f"benchrun: unknown task {tid!r} (BENCH_DIR={bench_dir()})\n")
            sys.exit(1)
        tasks.append(Task(tid, kind, lb, hard_ab))
    if not tasks:
        sys.stderr.write("benchrun: spec.json has no tasks\n")
        sys.exit(1)
    return tasks


# ---------------------------------------------------------------- sample runners

def agent_timed_out(ag):
    """llm_bench.run_agent writes "TIMEOUT" as the whole agent log when the
    agent subprocess ran out of time; the result dict itself has no error key,
    so the log file is the only way to see it (no answer = not a score)."""
    ws = ag.get("workspace")
    if not ws:
        return False
    try:
        return (Path(ws) / "_agent_log.txt").read_text(
            encoding="utf-8", errors="replace").strip() == "TIMEOUT"
    except OSError:
        return False


def fail_reason(r):
    """Why this attempt is not a score, or None when it is. A failed request or
    a timed-out agent run says nothing about the model's quality."""
    if r.get("error"):
        return str(r["error"])[:200]
    if r.get("timeout"):
        return "agent run timed out"
    return None


def run_sample(lb, task, s, url, model, max_tokens, label):
    """Ask for one sample and grade it with the existing bench code.
    Returns (chat_result, passed, total, tail, answer_text_or_None)."""
    if task.kind == "agent":
        ag = lb.run_agent(label, model)  # Qwen Code on the host; seed is not settable
        r = {"total_s": ag.get("seconds"), "error": ag.get("error"),
             "timeout": agent_timed_out(ag)}
        return r, ag.get("passed") or 0, ag.get("total") or 0, ag.get("tail") or "", None
    lb.SAMPLING["seed"] = s  # seed = sample number, same seeds for before/after runs
    if task.kind == "longctx":
        r = lb.chat(url, model, lb.longctx_prompt(task.chars, s - 1), max_tokens=max_tokens)
        content = r.pop("content", "") or ""
        p, t = lb.grade_longctx(content)
        return r, p, t, content[-300:], content
    prompt = task.prompt
    r = lb.chat(url, model, prompt, max_tokens=max_tokens)
    content = r.pop("content", "") or ""
    if task.kind == "hard":
        p, t, tail = task.hard_ab.grade(task.dir, content)
        return r, p, t, tail, content
    # code / bugfix: graded exactly like llm_bench.cmd_run does
    code = lb.extract_code(content)
    if code:
        p, t, tail = lb.run_tests(task.test, {"solution.py": code, **task.extra})
        t = t or lb.count_tests(task.test)
    else:
        p, t, tail = 0, lb.count_tests(task.test), "no code block"
    return r, p, t, tail, content


CHAT_METRICS = ("completion_tokens", "reasoning_chars", "prompt_tokens", "decode_tps",
                "prefill_tps", "ttft_s", "total_s", "finish", "error")


def sample_record(tid, s, seed, passed, total, r, tail):
    rec = {"task": tid, "sample": s, "seed": seed, "passed": passed, "total": total,
           "score": (round(passed / total, 4) if total else 0)}
    for k in CHAT_METRICS:
        if k in r:
            rec[k] = r[k]
    rec["tail"] = (tail or "")[-300:]
    rec["t"] = int(time.time())
    return rec


# ---------------------------------------------------------------- main run

def read_done(samples_path):
    """(task, sample) pairs already graded; a torn last line is ignored (resume
    will redo that pair)."""
    done = set()
    try:
        with open(samples_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    done.add((str(rec["task"]), int(rec["sample"])))
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return done


def write_json_atomic(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(path)


def main():
    ap = argparse.ArgumentParser(prog="benchrun")
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("run_dir")
    a = ap.parse_args()
    run_dir = Path(a.run_dir)

    try:
        spec = json.loads((run_dir / "spec.json").read_text(encoding="utf-8"))
    except Exception as ex:
        sys.stderr.write(f"benchrun: cannot read {run_dir}/spec.json: {ex}\n")
        return 1
    n_samples = max(1, int(spec.get("samples") or 1))
    max_tokens = int(spec.get("max_tokens") or 32768)
    waits = retry_waits()
    url = spec.get("url") or DEFAULT_URL
    label = re.sub(r"[^A-Za-z0-9._-]", "-", str(spec.get("label") or "run"))[:60] or "run"

    bd = bench_dir()
    sys.path.insert(0, bd)
    sys.path.insert(0, os.path.join(bd, "hard"))
    import llm_bench as lb          # noqa: E402  (grading must stay in the bench code)
    import hard_ab                  # noqa: E402

    tasks = resolve_tasks(spec, lb, hard_ab)
    env = env_snapshot(url)         # exits 2 if the server is down
    model = env["model"]
    env_p = run_dir / "env.json"
    if not env_p.exists():          # once at the start; a resume keeps the original
        write_json_atomic(env_p, env)

    warm = lb.chat(url, model, "Reply with OK.", 64)  # warm-up, not counted (as the old scripts)
    if warm.get("error"):
        sys.stderr.write(f"benchrun: the server at {url} did not answer the warm-up "
                         f"request ({warm['error']}).\n")
        return 2

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # keep what is saved

    answers = run_dir / "answers"
    answers.mkdir(exist_ok=True)
    samples_p = run_dir / "samples.jsonl"
    progress_p = run_dir / "progress.json"
    done = read_done(samples_p)
    total = len(tasks) * n_samples
    secs_by_task, all_secs = {}, []
    # seed the ETA tables from samples already on disk (resume case)
    try:
        for line in samples_p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            v = rec.get("total_s")
            if isinstance(v, (int, float)):
                secs_by_task.setdefault(str(rec.get("task")), []).append(float(v))
                all_secs.append(float(v))
    except OSError:
        pass
    task_secs = {t: (sum(v) / len(v)) for t, v in secs_by_task.items()}

    def write_progress(current):
        eta = None
        if all_secs:
            mean_all = sum(all_secs) / len(all_secs)
            eta = 0.0
            for s2 in range(1, n_samples + 1):
                for t2 in tasks:
                    if (t2.id, s2) not in done:
                        eta += task_secs.get(t2.id, mean_all)
            eta = round(eta)
        write_json_atomic(progress_p, {"done": len(done), "total": total,
                                       "current": current, "eta_s": eta})

    print(f"benchrun {spec.get('id') or run_dir.name}: {len(tasks)} tasks x {n_samples} samples "
          f"on {url} model {model} ({len(done)}/{total} already done)", flush=True)
    write_progress("")
    for s in range(1, n_samples + 1):
        for task in tasks:
            if (task.id, s) in done:
                continue
            current = f"{task.id} sample {s}"
            seed = None if task.kind == "agent" else s
            r = {}
            bad = None
            for attempt in range(len(waits) + 1):
                r, p, t, tail, content = run_sample(lb, task, s, url, model,
                                                    max_tokens, label)
                if r.get("error") == "qwen not found":
                    sys.stderr.write(
                        "benchrun: the agent task needs the qwen command, which is "
                        "not installed - every agent sample would fail, so the run "
                        "stops now (nothing was recorded).\n")
                    return 1
                bad = fail_reason(r)
                if not bad:
                    break
                if attempt < len(waits):
                    wait = waits[attempt]
                    write_progress(f"{current}: not a score ({bad}) - retrying "
                                   f"in {int(wait)}s")
                    time.sleep(wait)
                    print(f"  {task.id:14s} s{s}: attempt {attempt + 1} failed "
                          f"({bad}); waited {int(wait)}s, /models "
                          f"{'answered' if server_up(url) else 'still down'}",
                          flush=True)
            if bad:
                # a failed request graded 0 would invent a regression out of a
                # server hiccup: pause instead and record nothing for this pair.
                write_progress("paused: the model server stopped answering")
                sys.stderr.write(
                    f"benchrun: {task.id} sample {s} failed {len(waits) + 1} times "
                    f"({bad}); the run is paused. Fix the server and press Resume - "
                    f"this sample is not in samples.jsonl.\n")
                return 3
            rec = sample_record(task.id, s, seed, p, t, r, tail)
            with open(samples_p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            done.add((task.id, s))
            if content is not None:
                try:
                    (answers / f"{task.id}_s{s}.md").write_text(content, encoding="utf-8")
                except OSError:
                    pass
            if isinstance(r.get("total_s"), (int, float)):
                secs_by_task.setdefault(task.id, []).append(float(r["total_s"]))
                task_secs[task.id] = sum(secs_by_task[task.id]) / len(secs_by_task[task.id])
                all_secs.append(float(r["total_s"]))
            write_progress(current)
            print(f"  {task.id:14s} s{s}: {p}/{t}  decode={r.get('decode_tps')} t/s "
                  f"{r.get('total_s')}s finish={r.get('finish')}"
                  + ("  (hit the token limit)" if r.get("finish") == "length" else ""),
                  flush=True)
    print(f"benchrun: complete ({len(done)}/{total})", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as ex:
        raise
    except KeyboardInterrupt:
        sys.exit(143)
